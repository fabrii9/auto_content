# -*- coding: utf-8 -*-

import base64
import json
import logging
import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import brand_pack, planner

_logger = logging.getLogger(__name__)


class AutoContentBrand(models.Model):
    _name = 'auto.content.brand'
    _description = 'Marca de contenido automatizado'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(string='Marca', required=True, tracking=True)
    code = fields.Char(
        string='Código',
        required=True,
        tracking=True,
        help='Se usa por WhatsApp para elegir la marca: #codigo.',
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True,
        default=lambda self: self.env.company, index=True,
    )

    pack_file = fields.Binary(string='Paquete de marca (.zip)', attachment=True)
    pack_filename = fields.Char(string='Archivo del paquete')
    pack_checksum = fields.Char(string='Checksum del paquete', readonly=True, copy=False)
    pack_loaded_at = fields.Datetime(string='Paquete cargado', readonly=True, copy=False)
    template_path = fields.Char(string='Plantilla', readonly=True)
    width = fields.Integer(string='Ancho (px)', readonly=True)
    height = fields.Integer(string='Alto (px)', readonly=True)
    designs_json = fields.Text(string='Diseños', readonly=True)
    video_json = fields.Text(string='Configuración de video', readonly=True,
                             help='Sección "video" del paquete. Si está, los videos se editan como montaje.')
    instructions = fields.Text(string='Instrucciones del paquete', readonly=True)
    knowledge = fields.Text(string='Conocimiento del paquete', readonly=True)
    test_photo_path = fields.Char(string='Foto de prueba', readonly=True)
    test_text = fields.Char(string='Texto de prueba', readonly=True)
    extra_instructions = fields.Text(
        string='Ajustes de instrucciones',
        help='Se suman a las instrucciones del paquete sin tener que volver a cargarlo.',
    )
    place_ids = fields.One2many('auto.content.place', 'brand_id', string='Lugares')

    ai_mode = fields.Selection(
        [('provider', 'Proveedor por API (LLM Connector)'), ('claude_code', 'Claude Code (worker del operador)')],
        string='Modo de IA', required=True, default='provider', tracking=True,
        help='Con Claude Code, las solicitudes esperan a que el worker (tools/claude_worker.py), '
             'corriendo en una PC con Claude Code logueado, las tome y devuelva el plan.',
    )
    claude_model = fields.Char(
        string='Modelo de Claude Code', default='sonnet',
        help='Alias o nombre completo que recibe claude --model (sonnet, opus, haiku...).',
    )
    llm_provider_id = fields.Many2one(
        'llm.provider', string='Proveedor de IA', ondelete='restrict',
        help='Tiene que ser OpenAI, Anthropic o compatible con OpenAI, y el modelo tiene que ver imágenes.',
    )
    llm_model = fields.Char(string='Modelo', default='gpt-4o')
    llm_temperature = fields.Float(string='Temperatura', default=0.4)
    llm_max_tokens = fields.Integer(string='Máximo de tokens', default=4000)

    request_ids = fields.One2many('auto.content.request', 'brand_id', string='Solicitudes')
    request_count = fields.Integer(compute='_compute_request_count')

    _code_unique = models.Constraint(
        'UNIQUE(code, company_id)',
        'Ya hay una marca con ese código en la compañía.',
    )

    @api.depends('request_ids')
    def _compute_request_count(self):
        for brand in self:
            brand.request_count = len(brand.request_ids)

    @api.constrains('code')
    def _check_code(self):
        for brand in self:
            if not re.fullmatch(r'[a-z0-9_-]{2,30}', brand.code or ''):
                raise ValidationError(_('El código va en minúsculas, sin espacios ni acentos (por ejemplo: capsic).'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('code'):
                vals['code'] = vals['code'].strip().lower()
        brands = super().create(vals_list)
        brands.filtered('pack_file')._load_pack()
        return brands

    def write(self, vals):
        if vals.get('code'):
            vals['code'] = vals['code'].strip().lower()
        result = super().write(vals)
        if vals.get('pack_file'):
            self._load_pack()
        return result

    # ------------------------------------------------------------------
    # Paquete de marca
    # ------------------------------------------------------------------
    def _pack_bytes(self):
        self.ensure_one()
        data = self.with_context(bin_size=False).pack_file
        if not data:
            raise UserError(_('La marca %s no tiene paquete cargado.') % self.name)
        return base64.b64decode(data)

    def _load_pack(self):
        Place = self.env['auto.content.place']
        for brand in self:
            try:
                manifest, texts, checksum = brand_pack.read_pack(brand._pack_bytes())
            except brand_pack.PackError as exc:
                raise UserError(_('Paquete de marca inválido: %s') % exc) from exc
            super(AutoContentBrand, brand).write({
                'pack_checksum': checksum,
                'pack_loaded_at': fields.Datetime.now(),
                'template_path': manifest['plantilla'],
                'width': manifest['ancho'],
                'height': manifest['alto'],
                'designs_json': json.dumps(manifest['disenos'], ensure_ascii=False, indent=2),
                'video_json': json.dumps(manifest['video'], ensure_ascii=False, indent=2) if manifest.get('video') else False,
                'instructions': texts['instrucciones'],
                'knowledge': texts['conocimiento'],
                'test_photo_path': manifest.get('foto_prueba') or False,
                'test_text': manifest.get('texto_prueba') or False,
            })
            existing = {place.name.lower(): place for place in brand.place_ids}
            for item in manifest.get('lugares', []):
                aliases = ', '.join(item.get('alias') or [])
                place = existing.get(item['nombre'].lower())
                if place:
                    if aliases and place.aliases != aliases:
                        place.aliases = aliases
                else:
                    Place.create({'brand_id': brand.id, 'name': item['nombre'], 'aliases': aliases})
            brand.message_post(body=_('Paquete de marca cargado (%s).') % (brand.pack_filename or checksum[:12]))

    def _pack_dir(self):
        self.ensure_one()
        if not self.pack_checksum:
            raise UserError(_('La marca %s no tiene paquete cargado.') % self.name)
        cache_key = '%s-%s' % (self.env.cr.dbname, self.id)
        return brand_pack.extract_pack(self._pack_bytes(), self.pack_checksum, cache_key)

    def _designs(self):
        self.ensure_one()
        return json.loads(self.designs_json or '{}')

    def _video_config(self):
        """La sección "video" del paquete con el tamaño de la pieza, o ``None``."""
        self.ensure_one()
        if not self.video_json:
            return None
        return dict(json.loads(self.video_json), ancho=self.width, alto=self.height)

    def _places(self):
        self.ensure_one()
        return [
            {'nombre': place.name, 'alias': place._alias_list()}
            for place in self.place_ids if place.active
        ]

    # ------------------------------------------------------------------
    # IA
    # ------------------------------------------------------------------
    def _prompts(self, texts, media, previous_plan=None, instruction=None, extra=None, montage=None):
        """``(system, user)`` para la IA; los mismos en los dos modos."""
        self.ensure_one()
        system_text = planner.build_system_prompt(
            self.name, self._designs(), self._places(), self.instructions, self.knowledge, self.extra_instructions,
            video=montage['cfg'] if montage else None,
        )
        return system_text, planner.build_user_prompt(texts, media, previous_plan, instruction, extra)

    def _ai_plan(self, texts, media, images, previous_plan=None, instruction=None, extra=None, montage=None):
        """Pide el plan al proveedor por API. ``images`` son JPEG en el orden de ``media``."""
        self.ensure_one()
        provider = self.llm_provider_id.sudo()
        if not provider:
            raise UserError(_('Configurá el proveedor de IA de la marca %s.') % self.name)
        designs = self._designs()
        system_text, user_text = self._prompts(texts, media, previous_plan, instruction, extra, montage)
        messages = planner.build_messages(provider.provider_type, system_text, user_text, images)
        extra = {}
        if provider.provider_type in ('openai', 'custom', 'kimi', 'groq'):
            extra['response_format'] = {'type': 'json_object'}
        answer = provider.chat_completion(
            messages, model=self.llm_model or None, temperature=self.llm_temperature,
            max_tokens=self.llm_max_tokens, **extra,
        )
        try:
            return planner.parse_plan(answer, designs, media, montage)
        except planner.PlanError as first_error:
            _logger.warning('auto_content: plan inválido (%s), reintento una vez.', first_error)
            messages += [
                {'role': 'assistant', 'content': answer or ''},
                {'role': 'user', 'content': 'Tu respuesta no sirve: %s. Respondé solo con el JSON pedido.' % first_error},
            ]
            answer = provider.chat_completion(
                messages, model=self.llm_model or None, temperature=self.llm_temperature,
                max_tokens=self.llm_max_tokens, **extra,
            )
            return planner.parse_plan(answer, designs, media, montage)

    def action_test(self):
        """Arma una pieza de prueba con la foto y el texto de prueba del paquete,
        sin WhatsApp, y abre la solicitud."""
        self.ensure_one()
        if not self.test_photo_path:
            raise UserError(_('El paquete no define "foto_prueba".'))
        photo = self._pack_dir() / self.test_photo_path
        request = self.env['auto.content.request'].create({
            'brand_id': self.id,
            'company_id': self.company_id.id,
            'state': 'queued',
        })
        request._add_inbound('test-%s' % uuid.uuid4().hex, 'text', body=self.test_text or _('Prueba'))
        attachment = self.env['ir.attachment'].create({
            'name': photo.name,
            'raw': photo.read_bytes(),
            'mimetype': 'image/png' if photo.suffix.lower() == '.png' else 'image/jpeg',
            'res_model': 'auto.content.request',
            'res_id': request.id,
        })
        request._add_inbound('test-%s' % uuid.uuid4().hex, 'image', attachment=attachment)
        request._run()
        return request._open_form()

    def action_view_requests(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('auto_content.action_auto_content_request')
        action['domain'] = [('brand_id', '=', self.id)]
        action['context'] = {'default_brand_id': self.id}
        return action


class AutoContentPlace(models.Model):
    _name = 'auto.content.place'
    _description = 'Lugar oficial de una marca'
    _order = 'brand_id, name'

    brand_id = fields.Many2one('auto.content.brand', string='Marca', required=True, ondelete='cascade', index=True)
    name = fields.Char(string='Nombre oficial', required=True)
    aliases = fields.Char(string='Otras formas de escribirlo', help='Separadas por coma.')
    active = fields.Boolean(default=True)

    def _alias_list(self):
        self.ensure_one()
        return [alias.strip() for alias in (self.aliases or '').split(',') if alias.strip()]
