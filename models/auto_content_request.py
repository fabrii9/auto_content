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

from ..services import planner, renderer
from ..services.whatsapp_out import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES

_logger = logging.getLogger(__name__)

MAX_MEDIA_FOR_AI = 6
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
        self.write({'state': 'processing', 'error_message': False})
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

    def _prepare_media(self, workdir):
        """Escribe el material en ``workdir``.

        Devuelve ``(texts, media, images)``: ``images`` son JPEG para la IA
        (un cuadro por video), en el orden de ``media`` y como máximo
        ``MAX_MEDIA_FOR_AI``.
        """
        texts, media = self._material()
        ffmpeg = None
        images = []
        for item in media:
            attachment = item['attachment']
            suffix = mimetypes.guess_extension(attachment.mimetype or '') or ('.mp4' if item['kind'] == 'video' else '.jpg')
            item['path'] = workdir / ('media-%d%s' % (item['index'], suffix))
            item['path'].write_bytes(attachment.raw)
            if len(images) >= MAX_MEDIA_FOR_AI:
                continue
            if item['kind'] == 'video':
                ffmpeg = ffmpeg or renderer.find_ffmpeg(self._param('ffmpeg_path'))
                frame = renderer.video_frame(item['path'], workdir / ('frame-%d.jpg' % item['index']), ffmpeg)
                images.append(planner.to_jpeg(frame.read_bytes()))
            else:
                images.append(planner.to_jpeg(attachment.raw))
        return texts, media, images

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
            texts, media, images = self._prepare_media(workdir)
            ai_media = self._ai_media(media)[:len(images)]
            if answer is None:
                plan = brand._ai_plan(texts, ai_media, images, self._previous_plan(), instruction)
                model = brand.llm_model
            else:
                plan = planner.parse_plan(answer, brand._designs(), ai_media)
                model = 'Claude Code · %s' % (worker or brand.claude_model or '')
            output, kind = self._render(plan, media, workdir)
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
        previous.filtered(lambda v: v.state == 'proposed').write({'state': 'superseded'})
        self.write({'current_version_id': version.id, 'pending_instruction': False, 'state': 'review'})
        self.message_post(
            body=Markup('<p>%s</p>') % (_('Versión %d armada con el diseño %s.') % (number, plan['diseno'])),
            attachment_ids=[version.output_attachment_id.copy({'res_model': self._name, 'res_id': self.id}).id],
        )
        if self.operator_id:
            version._send_proposal()
        return version

    def _render(self, plan, media, workdir):
        """Devuelve ``(bytes, 'image'|'video')``."""
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
            return out.read_bytes(), 'video'
        if chosen:
            data['foto'] = chosen['path'].resolve().as_uri()
        out = renderer.render_html(template, data, workdir / 'pieza.png', brand.width, brand.height, chrome)
        return out.read_bytes(), 'image'

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
            try:
                texts, media, images = request._prepare_media(workdir)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            system_text, user_text = brand._prompts(
                texts, request._ai_media(media)[:len(images)], request._previous_plan(), request.pending_instruction,
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
            })
        return jobs

    @api.model
    def worker_submit(self, request_id, answer, worker=None):
        """Recibe la respuesta del worker. Si el JSON no sirve, devuelve el error
        para que el worker lo corrija; si sirve, arma la pieza y la manda."""
        self._check_worker()
        request = self.browse(request_id).exists()
        if not request or request.state != 'processing':
            return {'ok': False, 'retry': False, 'error': 'La solicitud ya no está tomada por el worker.'}
        _texts, media = request._material()
        try:
            planner.parse_plan(answer, request.brand_id._designs(), request._ai_media(media))
        except planner.PlanError as exc:
            return {'ok': False, 'retry': True, 'error': str(exc)}
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
        if self.sticker:
            lines += ['', _('Sticker sugerido: %s') % self.sticker]
        return '\n'.join(lines)

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
            if size <= MAX_VIDEO_BYTES:
                uids.append(sender.media(attachment, 'video', caption=caption))
            else:
                uids.append(sender.media(attachment, 'document', caption=caption))
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
