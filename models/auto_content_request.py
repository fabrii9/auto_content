# -*- coding: utf-8 -*-

import base64
import json
import logging
import mimetypes
import shutil
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi

from ..services import planner, renderer, video_edit
from ..services.whatsapp_out import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES

_logger = logging.getLogger(__name__)

MAX_MEDIA_FOR_AI = 6
# Montajes: fotos + hojas de contacto que ve la IA.
MAX_IMAGES_MONTAGE = 12
MAX_PHOTOS_MONTAGE = 4
VIDEO_DEBOUNCE = 180
STALE_PROCESSING = timedelta(minutes=20)
REQUEST_STATES = [
    ('collecting', 'Recibiendo'),
    ('brand', 'Esperando marca'),
    ('queued', 'En cola'),
    ('waiting_ai', 'Esperando IA'),
    ('processing', 'Armando'),
    ('review', 'Para revisar'),
    ('feedback', 'Esperando cambios'),
    ('approved', 'Aprobada'),
    ('discarded', 'Descartada'),
    ('error', 'Error'),
]


class AutoContentRequest(models.Model):
    _name = 'auto.content.request'
    _description = 'Solicitud de contenido automatizado'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(string='Solicitud', required=True, readonly=True, copy=False, default='/')
    operator_id = fields.Many2one('auto.content.operator', string='Operador', readonly=True, index=True)
    brand_id = fields.Many2one('auto.content.brand', string='Marca', index=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True,
        default=lambda self: self.env.company, index=True,
    )
    state = fields.Selection(REQUEST_STATES, string='Estado', default='collecting', required=True,
                             index=True, tracking=True)
    process_after = fields.Datetime(string='Armar desde', index=True)
    inbound_ids = fields.One2many('auto.content.message', 'request_id', string='Mensajes recibidos')
    media_count = fields.Integer(string='Archivos', compute='_compute_media_count')
    version_ids = fields.One2many('auto.content.version', 'request_id', string='Versiones')
    current_version_id = fields.Many2one('auto.content.version', string='Versión actual', readonly=True)
    preview = fields.Binary(related='current_version_id.preview', string='Vista previa')
    pending_instruction = fields.Text(string='Cambio pedido', readonly=True)
    error_message = fields.Text(string='Error', readonly=True)
    analysis_json = fields.Text(string='Análisis de los videos', readonly=True, copy=False,
                                help='Duración, cambios de toma y hojas de contacto de cada video (se calcula una vez).')
    transcript_json = fields.Text(string='Transcripción', readonly=True, copy=False,
                                  help='Lo que se dice en los videos, con tiempos (la hace el worker con Whisper).')
    pending_answer = fields.Text(string='Plan por armar', readonly=True, copy=False)
    pending_worker = fields.Char(string='Worker del plan', readonly=True, copy=False)

    @api.depends('inbound_ids.attachment_id')
    def _compute_media_count(self):
        for request in self:
            request.media_count = len(request.inbound_ids.filtered('attachment_id'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('auto.content.request') or '/'
        return super().create(vals_list)

    def _open_form(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Material recibido
    # ------------------------------------------------------------------
    @api.model
    def _fetch_inbound_attachment(self, account, msg_uid, media, request):
        """Copia el archivo que guardó el módulo de WhatsApp (o lo baja de Meta)."""
        Attachment = self.env['ir.attachment'].sudo()
        wa_message = self.env['whatsapp.message'].sudo().search([('msg_uid', '=', msg_uid)], limit=1)
        original = wa_message.mail_message_id.attachment_ids[:1]
        mime = media.get('mime_type') or (original.mimetype if original else 'application/octet-stream')
        if original:
            raw, name = original.raw, original.name
        else:
            raw = WhatsAppApi(account)._get_whatsapp_document(media['id'])
            name = media.get('filename') or ('archivo' + (mimetypes.guess_extension(mime.split(';')[0]) or ''))
        return Attachment.create({
            'name': name,
            'raw': raw,
            'mimetype': mime.split(';')[0],
            'res_model': self._name,
            'res_id': request.id,
        })

    def _add_inbound(self, msg_uid, kind, attachment=None, body=None, counts_as_material=True):
        self.ensure_one()
        return self.env['auto.content.message'].sudo().create({
            'msg_uid': msg_uid,
            'request_id': self.id,
            'operator_id': self.operator_id.id,
            'kind': kind,
            'body': body or False,
            'attachment_id': attachment.id if attachment else False,
            'is_material': counts_as_material,
        })

    def _bump(self):
        for request in self:
            wait = request.operator_id.debounce_seconds or 90
            if 'video' in request.inbound_ids.mapped('kind'):
                # Los videos tardan en subir: se espera más para juntar todos.
                wait = max(wait, VIDEO_DEBOUNCE)
            request.process_after = fields.Datetime.now() + timedelta(seconds=wait)

    def _material(self):
        """Textos y archivos del material, en orden de llegada."""
        self.ensure_one()
        messages = self.inbound_ids.filtered('is_material').sorted('id')
        texts = [m.body for m in messages if m.body]
        media = []
        for message in messages.filtered('attachment_id'):
            media.append({
                'index': len(media) + 1,
                'kind': 'video' if message.kind == 'video' else 'image',
                'attachment': message.attachment_id,
            })
        return texts, media

    # ------------------------------------------------------------------
    # Cola
    # ------------------------------------------------------------------
    @api.model
    def _cron_process(self):
        Cron = self.env['ir.cron']
        now = fields.Datetime.now()

        stale = self.search([('state', '=', 'processing'), ('write_date', '<', now - STALE_PROCESSING)])
        stale.write({'state': 'queued'})

        for request in self.search([('state', '=', 'collecting'), ('process_after', '<=', now)]):
            request._close_collection()
        Cron._commit_progress()

        queued = self.search([('state', '=', 'queued')], order='id', limit=5)
        Cron._commit_progress(remaining=len(queued))
        for request in queued:
            request._run()
            if Cron._commit_progress(1) < 120:
                break

    def _close_collection(self):
        self.ensure_one()
        texts, media = self._material()
        if not media and not any(t.strip() for t in texts):
            self.state = 'discarded'
            return
        brand = self.brand_id or self.operator_id.brand_id
        if brand:
            self.write({'brand_id': brand.id, 'state': 'queued'})
            return
        self.state = 'brand'
        self._ask_brand()

    def _ask_brand(self):
        self.ensure_one()
        operator = self.operator_id
        brands = self.env['auto.content.brand'].sudo().search([('company_id', '=', self.company_id.id)])
        if not operator:
            return
        if 0 < len(brands) <= 3:
            try:
                operator._sender().buttons(
                    _('¿De qué marca es %s?') % self.name,
                    [('ac:brand:%d:%d' % (self.id, brand.id), brand.name) for brand in brands],
                )
                return
            except Exception:
                _logger.exception('auto_content: no pude mandar los botones de marca.')
        operator._notify(_('¿De qué marca es %s? Escribí su código.\n\n%s') % (self.name, operator._help_text()))

    def _run(self, answer=None, worker=None):
        """Arma una versión y la manda. Los errores quedan en la solicitud."""
        self.ensure_one()
        if answer is None and self.pending_answer:
            answer, worker = self.pending_answer, self.pending_worker
        self.write({'state': 'processing', 'error_message': False, 'pending_answer': False, 'pending_worker': False})
        self.env['ir.cron']._commit_progress()
        try:
            with self.env.cr.savepoint():
                self._build_version(answer=answer, worker=worker)
        except Exception as exc:
            _logger.exception('auto_content: falló %s.', self.name)
            message = str(exc) or exc.__class__.__name__
            self.write({'state': 'error', 'error_message': message})
            self.message_post(body=_('Error: %s') % message)
            if self.operator_id:
                self.operator_id._notify(_(
                    'No pude armar %(name)s: %(error)s\n\nEscribí "reintentar" para probar de nuevo.'
                ) % {'name': self.name, 'error': message[:500]})

    def _write_media(self, workdir):
        """Escribe el material en ``workdir`` y devuelve ``(texts, media)``."""
        texts, media = self._material()
        for item in media:
            attachment = item['attachment']
            suffix = mimetypes.guess_extension(attachment.mimetype or '') or ('.mp4' if item['kind'] == 'video' else '.jpg')
            item['path'] = workdir / ('media-%d%s' % (item['index'], suffix))
            item['path'].write_bytes(attachment.raw)
        return texts, media

    def _prepare_media(self, workdir):
        """Escribe el material en ``workdir``.

        Devuelve ``(texts, media, images)``: ``images`` son JPEG para la IA
        (un cuadro por video), en el orden de ``media`` y como máximo
        ``MAX_MEDIA_FOR_AI``.
        """
        texts, media = self._write_media(workdir)
        ffmpeg = None
        images = []
        for item in media[:MAX_MEDIA_FOR_AI]:
            if item['kind'] == 'video':
                ffmpeg = ffmpeg or renderer.find_ffmpeg(self._param('ffmpeg_path'))
                frame = renderer.video_frame(item['path'], workdir / ('frame-%d.jpg' % item['index']), ffmpeg)
                images.append(planner.to_jpeg(frame.read_bytes()))
            else:
                images.append(planner.to_jpeg(item['attachment'].raw))
        return texts, media, images

    # ------------------------------------------------------------------
    # Montaje de videos
    # ------------------------------------------------------------------
    def _is_montage(self, media):
        """Con videos y una marca con sección "video", se edita un montaje."""
        self.ensure_one()
        return bool(self.brand_id._video_config()) and any(m['kind'] == 'video' for m in media)

    def _video_analysis(self, media, workdir):
        """Analiza cada video una sola vez (duración, cambios de toma y hojas de
        contacto, que quedan como adjuntos) y lo guarda en la solicitud."""
        self.ensure_one()
        analysis = json.loads(self.analysis_json or '{}')
        videos = analysis.setdefault('videos', {})
        ffmpeg = ffprobe = None
        changed = False
        for item in media:
            key = str(item['index'])
            if item['kind'] != 'video' or key in videos:
                continue
            ffmpeg = ffmpeg or renderer.find_ffmpeg(self._param('ffmpeg_path'))
            ffprobe = ffprobe or video_edit.find_ffprobe(ffmpeg)
            info = video_edit.probe(item['path'], ffprobe)
            info['escenas'] = video_edit.scene_changes(item['path'], ffmpeg)
            info['hojas'] = []
            sheets = video_edit.contact_sheets(item['path'], info['duracion'], workdir, 'video-%s' % key, ffmpeg)
            for number, sheet in enumerate(sheets, 1):
                attachment = self.env['ir.attachment'].sudo().create({
                    'name': '%s-video%s-hoja%d.jpg' % (self.name.replace('/', ''), key, number),
                    'raw': sheet['path'].read_bytes(),
                    'mimetype': 'image/jpeg',
                    'res_model': self._name,
                    'res_id': self.id,
                })
                info['hojas'].append({'adjunto': attachment.id, 'desde': sheet['desde'],
                                      'hasta': sheet['hasta'], 'paso': sheet['paso']})
            videos[key] = info
            changed = True
        if changed:
            self.analysis_json = json.dumps(analysis, ensure_ascii=False)
        return analysis

    def _montage_context(self, media, analysis=None):
        """Lo que necesita ``planner.parse_montage`` (sin leer los videos)."""
        self.ensure_one()
        analysis = analysis or json.loads(self.analysis_json or '{}')
        indexes = {str(m['index']) for m in media if m['kind'] == 'video'}
        return {
            'cfg': self.brand_id._video_config(),
            'videos': {k: v for k, v in (analysis.get('videos') or {}).items() if k in indexes},
            'fotos': [m['index'] for m in media if m['kind'] == 'image'],
        }

    def _transcripts(self):
        self.ensure_one()
        return json.loads(self.transcript_json or '{}')

    def _store_transcripts(self, transcripts):
        """Guarda las transcripciones del worker: ``{número de video: {'segmentos': [...]}}``."""
        self.ensure_one()
        if not isinstance(transcripts, dict):
            return
        current = self._transcripts()
        for key, value in transcripts.items():
            if isinstance(value, dict) and isinstance(value.get('segmentos'), list):
                current[str(key)] = value
        self.transcript_json = json.dumps(current, ensure_ascii=False)

    def _montage_inputs(self, media, analysis):
        """Fotos y hojas de contacto para la IA, la descripción de cada archivo
        y el texto con los cambios de toma y lo que se dice."""
        self.ensure_one()
        images, ai_media = [], []
        photos = [m for m in media if m['kind'] == 'image'][:MAX_PHOTOS_MONTAGE]
        videos = [m for m in media if m['kind'] == 'video']
        per_video = max(1, (MAX_IMAGES_MONTAGE - len(photos)) // max(1, len(videos)))
        for item in media:
            if item['kind'] == 'image':
                if item in photos:
                    images.append(planner.to_jpeg(item['attachment'].raw))
                    detail = _('foto (imagen %d)') % len(images)
                else:
                    detail = _('foto (no te la muestro: hay demasiadas)')
                ai_media.append({'index': item['index'], 'kind': 'image', 'detalle': detail})
                continue
            info = analysis['videos'][str(item['index'])]
            numbers = []
            for sheet in info['hojas'][:per_video]:
                attachment = self.env['ir.attachment'].sudo().browse(sheet['adjunto']).exists()
                if attachment:
                    images.append(attachment.raw)
                    numbers.append(str(len(images)))
            shape = _('vertical') if info['alto'] >= info['ancho'] else _('horizontal')
            detail = _('video de %(dur).1f s, %(shape)s (%(w)d×%(h)d), %(audio)s.') % {
                'dur': info['duracion'], 'shape': shape, 'w': info['ancho'], 'h': info['alto'],
                'audio': _('con sonido') if info['audio'] else _('sin sonido')}
            if info['escenas']:
                detail += ' ' + _('Cambios de toma en: %s s.') % ', '.join('%.1f' % t for t in info['escenas'])
            if numbers:
                detail += ' ' + _('Hojas de contacto: imágenes %(n)s (un cuadro cada %(step).1f s).') % {
                    'n': ', '.join(numbers), 'step': info['hojas'][0]['paso']}
            ai_media.append({'index': item['index'], 'kind': 'video', 'detalle': detail})
        extra = planner.describe_transcripts(self._transcripts())
        return images, ai_media, extra

    def _render_montage(self, plan, media, workdir):
        """Arma el montaje: placas y capas con Chrome, video con ffmpeg."""
        from PIL import Image, ImageOps

        brand = self.brand_id
        cfg = brand._video_config()
        edit = plan['montaje']
        pack = brand._pack_dir()
        template = pack / brand.template_path
        chrome = renderer.find_chrome(self._param('chrome_path'))
        ffmpeg = renderer.find_ffmpeg(self._param('ffmpeg_path'))
        width, height = brand.width, brand.height
        overlap = float(cfg.get('transicion_segundos') or 0.4)
        analysis = json.loads(self.analysis_json or '{}').get('videos') or {}

        clips = [dict(clip) for clip in edit['clips']]
        if edit['cierre']:
            card = renderer.render_html(
                template, dict(edit['cierre']['datos'], plantilla=edit['cierre']['diseno']),
                workdir / 'cierre.png', width, height, chrome)
            length = edit['cierre']['segundos']
            clips.append({'tipo': 'placa', 'archivo': None, 'imagen': str(card), 'desde': 0.0, 'hasta': length,
                          'duracion': length, 'velocidad': 1.0, 'estabilizar': False, 'encuadre': 0.5,
                          'rotulo': None,
                          'transicion': 'fundido' if 'fundido' in (cfg.get('transiciones') or []) else 'corte'})
        starts, total = video_edit.timeline(clips, overlap)

        capa = capa_until = None
        if edit['capa'] != 'ninguna':
            capa = renderer.render_html(
                template, dict(plan['datos'], plantilla=plan['diseno'], foto='', transparente=True),
                workdir / 'capa.png', width, height, chrome, transparent=True)
            content_end = starts[-1] if edit['cierre'] else total
            capa_until = min(4.0, content_end) if edit['capa'] == 'inicio' else content_end
        rotulos = {}
        if cfg.get('rotulo'):
            for i, clip in enumerate(clips):
                if clip.get('rotulo'):
                    rotulos[i] = renderer.render_html(
                        template, {'plantilla': cfg['rotulo'], 'texto': clip['rotulo'], 'transparente': True},
                        workdir / ('rotulo-%d.png' % i), width, height, chrome, transparent=True)

        sources = {}
        for item in media:
            if item['kind'] == 'video':
                info = analysis.get(str(item['index'])) or {}
                sources[item['index']] = {'path': item['path'], 'audio': info.get('audio', True)}
            else:
                # ffmpeg no aplica la orientación EXIF de las fotos.
                upright = workdir / ('foto-%d.jpg' % item['index'])
                with Image.open(item['path']) as image:
                    ImageOps.exif_transpose(image).convert('RGB').save(upright, format='JPEG', quality=92)
                sources[item['index']] = {'path': upright, 'audio': False}

        music = None
        track = next((t for t in cfg.get('musica') or [] if t['id'] == edit['musica']), None)
        if track:
            music = {'path': pack / track['archivo'], 'volumen': track.get('volumen', 0.25)}
        style = cfg.get('subtitulos') or {}
        out, starts, total, events = video_edit.render_montage(
            clips, sources, workdir / 'pieza.mp4', width, height, ffmpeg, workdir,
            audio_mode=edit['audio'], music=music, capa=capa, capa_hasta=capa_until, rotulos=rotulos,
            transcripts=self._transcripts() if edit['subtitulos'] else None, subtitle_style=style,
            font_path=pack / style['fuente'] if style.get('fuente') else None, default_transition=overlap,
        )
        preview = None
        if out.stat().st_size > MAX_VIDEO_BYTES:
            preview = video_edit.make_preview(out, workdir / 'vista.mp4', ffmpeg).read_bytes()
        script = self._montage_script(clips, starts, total, edit, events)
        return out.read_bytes(), 'video', {'preview': preview, 'script': script}

    def _montage_script(self, clips, starts, total, edit, events):
        """El guion legible que recibe el operador para pedir cambios."""
        lines = [_('Guion (%.1f s):') % total]
        for number, (clip, start) in enumerate(zip(clips, starts), 1):
            parts = [video_edit.format_time(start)]
            if number > 1 and clip['transicion'] != 'corte':
                parts.append(clip['transicion'])
            if clip['tipo'] == 'video':
                parts.append(_('video %(n)d de %(a).1f a %(b).1f s') % {
                    'n': clip['archivo'], 'a': clip['desde'], 'b': clip['hasta']})
                if clip['velocidad'] != 1:
                    parts.append(_('velocidad ×%g') % clip['velocidad'])
                if clip['estabilizar']:
                    parts.append(_('estabilizado'))
            elif clip['tipo'] == 'foto':
                parts.append(_('foto %(n)d (%(d).1f s)') % {'n': clip['archivo'], 'd': clip['duracion']})
            else:
                parts.append(_('placa de cierre (%.1f s)') % clip['duracion'])
            if clip.get('rotulo'):
                parts.append('«%s»' % clip['rotulo'])
            lines.append('%d. %s' % (number, ' · '.join(parts)))
        audio = {'original': _('sonido original'), 'mezcla': _('sonido original + música'),
                 'musica': _('solo música')}[edit['audio']]
        if edit['musica']:
            audio += ' (%s)' % edit['musica']
        lines.append(_('Audio: %s') % audio)
        if edit['capa'] != 'ninguna':
            lines.append(_('Capa de marca: %s') % (_('al inicio') if edit['capa'] == 'inicio' else _('todo el video')))
        if edit['subtitulos']:
            lines.append(_('Subtítulos: %d frases') % len(events) if events else _('Subtítulos: no hay transcripción'))
        return '\n'.join(lines)

    def _ai_media(self, media):
        return [{'index': m['index'], 'kind': m['kind']} for m in media][:MAX_MEDIA_FOR_AI]

    def _previous_plan(self):
        previous = self.current_version_id
        return json.loads(previous.plan_json) if previous and previous.plan_json else None

    def _build_version(self, answer=None, worker=None):
        """Arma una versión.

        Sin ``answer`` le pide el plan al proveedor de la marca, o, en modo
        Claude Code, deja la solicitud esperando al worker. Con ``answer`` (la
        respuesta del worker) valida ese plan y arma la pieza.
        """
        self.ensure_one()
        brand = self.brand_id
        if not brand:
            raise UserError(_('La solicitud no tiene marca.'))
        previous = self.current_version_id
        instruction = self.pending_instruction
        if self.operator_id and not previous and answer is None:
            self.operator_id._notify(_('Armando %(name)s para %(brand)s con %(count)s archivo(s)…') % {
                'name': self.name, 'brand': brand.name, 'count': self.media_count})
        if answer is None and brand.ai_mode == 'claude_code':
            self.state = 'waiting_ai'
            return self.env['auto.content.version']

        started = time.monotonic()
        workdir = Path(tempfile.mkdtemp(prefix='auto-content-'))
        try:
            texts, media = self._write_media(workdir)
            montage = None
            if self._is_montage(media):
                analysis = self._video_analysis(media, workdir)
                montage = self._montage_context(media, analysis)
            if answer is None:
                if montage:
                    images, ai_media, extra = self._montage_inputs(media, analysis)
                else:
                    _texts, _media, images = self._prepare_media(workdir)
                    ai_media, extra = self._ai_media(media)[:len(images)], None
                plan = brand._ai_plan(texts, ai_media, images, self._previous_plan(), instruction, extra, montage)
                model = brand.llm_model
            else:
                plan = planner.parse_plan(answer, brand._designs(), self._ai_media(media), montage)
                model = 'Claude Code · %s' % (worker or brand.claude_model or '')
            output, kind, extras = self._render(plan, media, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        number = len(self.version_ids) + 1
        version = self.env['auto.content.version'].sudo().create({
            'request_id': self.id,
            'sequence': number,
            'instruction': instruction or False,
            'design': plan['diseno'],
            'plan_json': json.dumps(plan, ensure_ascii=False, indent=2),
            'summary': plan['resumen'],
            'alerts': '\n'.join(plan['alertas']) or False,
            'place': plan['lugar'] or False,
            'sticker': plan['sticker'] or False,
            'caption': plan['texto_publicacion'] or False,
            'output_kind': kind,
            'script': extras.get('script') or False,
            'llm_model': model,
            'duration': time.monotonic() - started,
        })
        extension = '.mp4' if kind == 'video' else '.png'
        version.output_attachment_id = self.env['ir.attachment'].sudo().create({
            'name': '%s-%s-v%d%s' % (brand.code, self.name.replace('/', ''), number, extension),
            'raw': output,
            'mimetype': 'video/mp4' if kind == 'video' else 'image/png',
            'res_model': version._name,
            'res_id': version.id,
        })
        if extras.get('preview'):
            version.preview_attachment_id = self.env['ir.attachment'].sudo().create({
                'name': '%s-%s-v%d-vista.mp4' % (brand.code, self.name.replace('/', ''), number),
                'raw': extras['preview'],
                'mimetype': 'video/mp4',
                'res_model': version._name,
                'res_id': version.id,
            })
        previous.filtered(lambda v: v.state == 'proposed').write({'state': 'superseded'})
        self.write({'current_version_id': version.id, 'pending_instruction': False, 'state': 'review'})
        self.message_post(
            body=Markup('<p>%s</p>') % (_('Versión %d armada con el diseño %s.') % (number, plan['diseno'])),
            attachment_ids=[version.output_attachment_id.copy({'res_model': self._name, 'res_id': self.id}).id],
        )
        if self.operator_id:
            # Si WhatsApp rechaza un envío, la versión ya armada no se pierde:
            # queda para revisar en Odoo y se le avisa al operador.
            try:
                with self.env.cr.savepoint():
                    version._send_proposal()
            except Exception as exc:
                _logger.exception('auto_content: no pude mandar la propuesta de %s.', self.name)
                self.message_post(body=_('No se pudo mandar la propuesta por WhatsApp: %s') % exc)
                self.operator_id._notify(_(
                    '%(name)s quedó armada, pero WhatsApp no aceptó un envío (%(error)s). '
                    'La podés ver en Odoo.') % {'name': self.name, 'error': str(exc)[:200]})
        return version

    def _render(self, plan, media, workdir):
        """Devuelve ``(bytes, 'image'|'video', extras)``."""
        if plan.get('montaje'):
            return self._render_montage(plan, media, workdir)
        brand = self.brand_id
        template = brand._pack_dir() / brand.template_path
        chrome = renderer.find_chrome(self._param('chrome_path'))
        data = dict(plan['datos'], plantilla=plan['diseno'])
        chosen = next((m for m in media if m['index'] == plan['media']), None)
        if chosen and chosen['kind'] == 'video':
            ffmpeg = renderer.find_ffmpeg(self._param('ffmpeg_path'))
            overlay = renderer.render_html(
                template, dict(data, foto='', transparente=True), workdir / 'capa.png',
                brand.width, brand.height, chrome, transparent=True,
            )
            out = renderer.compose_video(chosen['path'], overlay, workdir / 'pieza.mp4',
                                         brand.width, brand.height, ffmpeg)
            return out.read_bytes(), 'video', {}
        if chosen:
            data['foto'] = chosen['path'].resolve().as_uri()
        out = renderer.render_html(template, data, workdir / 'pieza.png', brand.width, brand.height, chrome)
        return out.read_bytes(), 'image', {}

    def _param(self, key):
        return self.env['ir.config_parameter'].sudo().get_param('auto_content.%s' % key) or None

    # ------------------------------------------------------------------
    # Worker de Claude Code (tools/claude_worker.py, por XML-RPC)
    # ------------------------------------------------------------------
    def _check_worker(self):
        if not self.env.user.has_group('auto_content.grupo_auto_content_admin'):
            raise AccessError(_('El worker tiene que usar un usuario administrador de Contenido automatizado.'))

    @api.model
    def worker_claim_jobs(self, limit=1):
        """Toma solicitudes que esperan IA y devuelve lo necesario para resolverlas.

        Una solicitud tomada pasa a "Armando". Si el worker no responde en
        ``STALE_PROCESSING``, la cola la vuelve a ofrecer.
        """
        self._check_worker()
        jobs = []
        for request in self.search([('state', '=', 'waiting_ai')], order='id', limit=max(1, min(limit, 5))):
            brand = request.brand_id
            workdir = Path(tempfile.mkdtemp(prefix='auto-content-job-'))
            audios = []
            try:
                texts, media = request._write_media(workdir)
                montage = extra = None
                if request._is_montage(media):
                    analysis = request._video_analysis(media, workdir)
                    montage = request._montage_context(media, analysis)
                    images, ai_media, extra = request._montage_inputs(media, analysis)
                    audios = request._audios_to_transcribe(media, analysis, workdir)
                else:
                    _texts, _media, images = request._prepare_media(workdir)
                    ai_media = request._ai_media(media)[:len(images)]
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            system_text, user_text = brand._prompts(
                texts, ai_media, request._previous_plan(), request.pending_instruction, extra, montage,
            )
            request.write({'state': 'processing', 'error_message': False})
            jobs.append({
                'id': request.id,
                'name': request.name,
                'brand': brand.name,
                'model': brand.claude_model or 'sonnet',
                'system': system_text,
                'user': user_text,
                'images': [base64.b64encode(image).decode() for image in images],
                # Videos con sonido sin transcribir: el worker los pasa por Whisper
                # y agrega lo que se dice al prompt.
                'audios': audios,
            })
        return jobs

    def _audios_to_transcribe(self, media, analysis, workdir):
        self.ensure_one()
        done = self._transcripts()
        ffmpeg = renderer.find_ffmpeg(self._param('ffmpeg_path'))
        audios = []
        for item in media:
            key = str(item['index'])
            if item['kind'] != 'video' or key in done or not analysis['videos'][key].get('audio'):
                continue
            path = video_edit.extract_audio(item['path'], workdir / ('audio-%s.ogg' % key), ffmpeg)
            if path:
                audios.append({'video': item['index'], 'datos': base64.b64encode(path.read_bytes()).decode()})
        return audios

    @api.model
    def worker_submit(self, request_id, answer, worker=None, transcripts=None):
        """Recibe la respuesta del worker. Si el JSON no sirve, devuelve el error
        para que el worker lo corrija; si sirve, arma la pieza y la manda.

        Un montaje tarda minutos: queda en cola con el plan y lo arma la tarea
        automática, así el worker no espera."""
        self._check_worker()
        request = self.browse(request_id).exists()
        if not request or request.state != 'processing':
            return {'ok': False, 'retry': False, 'error': 'La solicitud ya no está tomada por el worker.'}
        if transcripts:
            request._store_transcripts(transcripts)
        _texts, media = request._material()
        montage = request._montage_context(media) if request._is_montage(media) else None
        try:
            plan = planner.parse_plan(answer, request.brand_id._designs(), request._ai_media(media), montage)
        except planner.PlanError as exc:
            return {'ok': False, 'retry': True, 'error': str(exc)}
        if montage:
            request.write({'state': 'queued', 'pending_answer': answer, 'pending_worker': worker or False})
            if request.operator_id:
                request.operator_id._notify(_('🎬 Editando el video de %(name)s (%(dur).0f s, %(clips)d clips). '
                                              'Tarda unos minutos.') % {
                    'name': request.name, 'dur': plan['montaje']['duracion'],
                    'clips': len(plan['montaje']['clips'])})
            return {'ok': True, 'retry': False, 'state': 'queued', 'error': False}
        request._run(answer=answer, worker=worker)
        return {'ok': request.state == 'review', 'retry': False, 'state': request.state,
                'error': request.error_message or False}

    @api.model
    def worker_fail(self, request_id, message):
        self._check_worker()
        request = self.browse(request_id).exists()
        if request and request.state == 'processing':
            request.write({'state': 'error', 'error_message': message})
            request.message_post(body=_('Error del worker: %s') % message)
            if request.operator_id:
                request.operator_id._notify(_(
                    'No pude armar %(name)s: %(error)s\n\nEscribí "reintentar" para probar de nuevo.'
                ) % {'name': request.name, 'error': message[:500]})
        return True

    # ------------------------------------------------------------------
    # Decisiones del operador
    # ------------------------------------------------------------------
    def _request_change(self, instruction):
        self.ensure_one()
        self.write({'pending_instruction': instruction, 'state': 'queued'})
        if self.operator_id:
            self.operator_id._notify(_('Dale, armo otra versión de %s.') % self.name)

    def _discard(self):
        for request in self:
            request.current_version_id.filtered(lambda v: v.state == 'proposed').write({'state': 'rejected'})
            request.state = 'discarded'
            if request.operator_id:
                request.operator_id._notify(_('%s descartada.') % request.name)

    def action_queue(self):
        for request in self.filtered(lambda r: r.state in ('error', 'discarded', 'brand') and r.brand_id):
            request.write({'state': 'queued', 'error_message': False})
        return True

    def action_run_now(self):
        self.ensure_one()
        if not self.brand_id:
            raise UserError(_('Elegí la marca primero.'))
        self._run()
        return self._open_form()

    def action_discard(self):
        self._discard()
        return True

    def action_approve(self):
        for request in self:
            if request.current_version_id and request.state in ('review', 'feedback'):
                request.current_version_id._approve()
        return True


class AutoContentVersion(models.Model):
    _name = 'auto.content.version'
    _description = 'Versión de una pieza'
    _order = 'request_id desc, sequence desc'

    request_id = fields.Many2one('auto.content.request', string='Solicitud', required=True,
                                 ondelete='cascade', index=True)
    brand_id = fields.Many2one(related='request_id.brand_id', store=True, string='Marca')
    sequence = fields.Integer(string='Versión', required=True)
    state = fields.Selection([
        ('proposed', 'Propuesta'),
        ('approved', 'Aprobada'),
        ('rejected', 'Rechazada'),
        ('superseded', 'Reemplazada'),
    ], string='Estado', default='proposed', required=True, index=True)
    instruction = fields.Text(string='Cambio pedido')
    design = fields.Char(string='Diseño')
    plan_json = fields.Text(string='Plan de la IA')
    summary = fields.Text(string='Resumen')
    alerts = fields.Text(string='Alertas')
    place = fields.Char(string='Lugar')
    sticker = fields.Char(string='Sticker sugerido')
    caption = fields.Text(string='Texto para publicar')
    output_kind = fields.Selection([('image', 'Imagen'), ('video', 'Video')], string='Tipo')
    output_attachment_id = fields.Many2one('ir.attachment', string='Archivo', readonly=True)
    preview_attachment_id = fields.Many2one('ir.attachment', string='Vista liviana', readonly=True,
                                            help='Video comprimido para mandarlo por WhatsApp (máximo 16 MB).')
    script = fields.Text(string='Guion del montaje')
    preview = fields.Binary(compute='_compute_preview', string='Vista previa')
    llm_model = fields.Char(string='Modelo')
    duration = fields.Float(string='Duración (s)', digits=(10, 1))
    wa_msg_uids = fields.Char(string='Mensajes de WhatsApp', readonly=True)
    approved_at = fields.Datetime(string='Aprobada el', readonly=True)

    @api.depends('output_attachment_id', 'output_kind')
    def _compute_preview(self):
        for version in self:
            attachment = version.output_attachment_id
            version.preview = attachment.datas if attachment and version.output_kind == 'image' else False

    def _proposal_body(self):
        self.ensure_one()
        request = self.request_id
        lines = ['*%s · %s · versión %d*' % (request.brand_id.name, request.name, self.sequence)]
        detail = _('Diseño: %s') % self.design
        if self.place:
            detail += ' · ' + _('Lugar: %s') % self.place
        lines.append(detail)
        if self.summary:
            lines += ['', self.summary]
        if self.alerts:
            lines += ['', _('⚠️ Revisá:')] + ['• %s' % a for a in self.alerts.splitlines()]
        if self.script:
            lines += ['', self.script]
        if self.sticker:
            lines += ['', _('Sticker sugerido: %s') % self.sticker]
        return '\n'.join(lines)[:4000]

    def _send_proposal(self):
        """Manda la pieza (vista previa + archivo en calidad original) y los botones."""
        self.ensure_one()
        request = self.request_id
        sender = request.operator_id._sender()
        attachment = self.output_attachment_id
        size = len(attachment.raw or b'')
        uids = []
        caption = '%s · v%d' % (request.name, self.sequence)
        if self.output_kind == 'video':
            light = attachment if size <= MAX_VIDEO_BYTES else self.preview_attachment_id
            if light:
                uids.append(sender.media(light, 'video', caption=caption))
            if light != attachment:
                # WhatsApp no acepta videos de más de 16 MB ni como documento:
                # el original va como link de descarga.
                uids.append(sender.text(_('%(caption)s en calidad original (%(mb).0f MB):\n%(url)s') % {
                    'caption': caption, 'mb': size / 1024 / 1024, 'url': self._download_url()}))
        else:
            if size <= MAX_IMAGE_BYTES:
                uids.append(sender.media(attachment, 'image', caption=caption))
            uids.append(sender.media(attachment, 'document', caption=_('%s en calidad original') % caption))
        # El texto de un mensaje con botones admite 1024 caracteres: el detalle
        # va en un texto aparte (4096) y los botones con una pregunta corta.
        uids.append(sender.text(self._proposal_body()))
        uids.append(sender.buttons(_('¿Qué hacemos con %s · v%d?') % (request.name, self.sequence), [
            ('ac:approve:%d' % self.id, _('Aprobar')),
            ('ac:change:%d' % self.id, _('Cambiar')),
            ('ac:discard:%d' % self.id, _('Descartar')),
        ]))
        self.wa_msg_uids = ' '.join(uid for uid in uids if uid)

    def _download_url(self):
        self.ensure_one()
        attachment = self.output_attachment_id.sudo()
        token = attachment.access_token or attachment.generate_access_token()[0]
        return '%s/web/content/%d?access_token=%s&download=true' % (attachment.get_base_url(), attachment.id, token)

    def _approve(self):
        for version in self:
            request = version.request_id
            version.write({'state': 'approved', 'approved_at': fields.Datetime.now()})
            request.state = 'approved'
            request.message_post(body=_('Versión %d aprobada.') % version.sequence)
            if request.operator_id:
                request.operator_id._notify(_('✅ %s aprobada. Queda lista para publicar.') % request.name)

    def action_download(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%d?download=true' % self.output_attachment_id.id,
            'target': 'self',
        }


class AutoContentMessage(models.Model):
    _name = 'auto.content.message'
    _description = 'Mensaje recibido por contenido automatizado'
    _order = 'id'

    msg_uid = fields.Char(string='ID de WhatsApp', required=True, index=True)
    request_id = fields.Many2one('auto.content.request', string='Solicitud', ondelete='cascade', index=True)
    operator_id = fields.Many2one('auto.content.operator', string='Operador', ondelete='set null')
    kind = fields.Selection([
        ('text', 'Texto'),
        ('image', 'Foto'),
        ('video', 'Video'),
        ('button', 'Botón'),
        ('other', 'Otro'),
    ], string='Tipo', required=True)
    body = fields.Text(string='Texto')
    attachment_id = fields.Many2one('ir.attachment', string='Archivo', ondelete='set null')
    is_material = fields.Boolean(string='Es material', default=True,
                                 help='Falso para respuestas como cambios o aprobaciones.')

    _msg_uid_unique = models.Constraint('UNIQUE(msg_uid)', 'Ese mensaje ya se procesó.')
