# -*- coding: utf-8 -*-

import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.whatsapp_out import WhatsAppSender

_logger = logging.getLogger(__name__)

APPROVE_WORDS = {'aprobar', 'aprobado', 'aprobada', 'ok', 'dale', 'si', 'sí', 'listo'}
DISCARD_WORDS = {'descartar', 'descartada', 'no', 'borrar'}
RETRY_WORDS = {'reintentar', 'reintenta', 'reintentá'}
HELP_WORDS = {'ayuda', 'help', '?', 'marcas', '#marcas', '#ayuda'}
SUPPORTED_MEDIA = ('image', 'video', 'document')


def phone_key(number):
    """Últimos 10 dígitos: iguala 5493777641061 y +54 3777 64-1061."""
    digits = re.sub(r'\D', '', number or '')
    return digits[-10:] if len(digits) >= 10 else digits


class AutoContentOperator(models.Model):
    _name = 'auto.content.operator'
    _description = 'Operador de contenido automatizado'
    _order = 'name'

    name = fields.Char(string='Nombre', required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True,
        default=lambda self: self.env.company,
    )
    phone = fields.Char(string='WhatsApp', required=True, help='El número desde el que reenvía el material.')
    phone_key = fields.Char(compute='_compute_phone_key', store=True, index=True)
    wa_account_id = fields.Many2one(
        'whatsapp.account', string='Cuenta de WhatsApp', required=True, ondelete='restrict',
        help='El número de Odoo al que el operador le escribe.',
    )
    wa_id = fields.Char(string='ID de WhatsApp', readonly=True, help='Como lo informa Meta en el último mensaje.')
    brand_id = fields.Many2one(
        'auto.content.brand', string='Marca activa',
        help='La marca de lo que mande a continuación. Se cambia por WhatsApp con #codigo.',
    )
    debounce_seconds = fields.Integer(
        string='Espera para juntar (s)', default=90,
        help='Segundos sin mensajes nuevos antes de armar la pieza con todo lo recibido.',
    )
    last_inbound_at = fields.Datetime(string='Último mensaje', readonly=True)

    _phone_unique = models.Constraint(
        'UNIQUE(phone_key, wa_account_id)',
        'Ese número ya es operador de esa cuenta de WhatsApp.',
    )

    @api.depends('phone')
    def _compute_phone_key(self):
        for operator in self:
            operator.phone_key = phone_key(operator.phone)

    @api.constrains('phone')
    def _check_phone(self):
        for operator in self:
            if len(operator.phone_key or '') < 10:
                raise ValidationError(_('Cargá el número completo, con código de área.'))

    @api.model_create_multi
    def create(self, vals_list):
        operators = super().create(vals_list)
        operators._mute_sales_agent()
        return operators

    def write(self, vals):
        result = super().write(vals)
        if 'phone' in vals or 'wa_account_id' in vals or vals.get('active'):
            self._mute_sales_agent()
        return result

    # ------------------------------------------------------------------
    # Aislamiento: el agente de ventas no le responde al operador
    # ------------------------------------------------------------------
    def _mute_sales_agent(self):
        """Apaga el agente de whatsapp_ai_agents en el chat del operador.

        Sin fecha de desactivación ese módulo no lo reactiva solo. Si el
        módulo no está instalado, el campo no existe y no hay nada que hacer.
        """
        Channel = self.env['discuss.channel'].sudo()
        if 'ai_agent_enabled' not in Channel._fields:
            return
        for operator in self.filtered('active'):
            channels = Channel.search([
                ('channel_type', '=', 'whatsapp'),
                ('whatsapp_number', '=like', '%' + operator.phone_key),
            ])
            channels.filtered(lambda c: c.ai_agent_enabled or c.ai_agent_disabled_date).write({
                'ai_agent_enabled': False,
                'ai_agent_disabled_date': False,
            })

    @api.model
    def _match(self, account, sender):
        key = phone_key(sender)
        if len(key) < 10:
            return self.browse()
        return self.sudo().search([
            ('phone_key', '=', key),
            ('wa_account_id', '=', account.id),
            ('active', '=', True),
        ], limit=1)

    # ------------------------------------------------------------------
    # Envíos
    # ------------------------------------------------------------------
    def _sender(self):
        self.ensure_one()
        number = self.wa_id or re.sub(r'\D', '', self.phone)
        return WhatsAppSender(self.wa_account_id, number)

    def _notify(self, body):
        """Manda un texto al operador. Nunca corta el flujo si falla."""
        self.ensure_one()
        try:
            self._sender().text(body)
            return True
        except Exception:
            _logger.exception('auto_content: no pude avisarle al operador %s.', self.name)
            return False

    def _help_text(self):
        self.ensure_one()
        brands = self.env['auto.content.brand'].sudo().search([('company_id', '=', self.company_id.id)])
        brand_list = ', '.join('#%s (%s)' % (b.code, b.name) for b in brands) or _('ninguna')
        return _(
            '*Contenido automatizado*\n'
            'Reenviame fotos o videos con el texto (por ejemplo, la zona). '
            'Cuando pasan %(wait)s s sin mensajes nuevos, armo la pieza y te la mando para revisar.\n\n'
            '• Marca activa: %(active)s\n'
            '• Para cambiarla, escribí el código: %(brands)s\n'
            '• En la propuesta: Aprobar, Cambiar o Descartar.\n'
            '• "reintentar" vuelve a intentar la última que falló.'
        ) % {
            'wait': self.debounce_seconds,
            'active': self.brand_id.name or _('ninguna'),
            'brands': brand_list,
        }

    def action_send_test(self):
        self.ensure_one()
        try:
            self._sender().text(_('Prueba de Contenido automatizado: este número quedó como operador.'))
        except Exception as exc:
            raise UserError(_(
                'No se pudo enviar: %s\n\nWhatsApp solo deja escribir si el operador mandó un mensaje en las últimas 24 h.'
            ) % exc) from exc
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'message': _('Mensaje enviado.')},
        }

    def action_mute_sales_agent(self):
        self._mute_sales_agent()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'message': _('Listo: el agente de ventas no le responde a este número.')},
        }

    # ------------------------------------------------------------------
    # Mensajes entrantes
    # ------------------------------------------------------------------
    def _handle_inbound(self, account, item):
        """Procesa un mensaje del webhook de Meta que mandó este operador."""
        self.ensure_one()
        msg_uid = item.get('id')
        Message = self.env['auto.content.message'].sudo()
        if not msg_uid or Message.search_count([('msg_uid', '=', msg_uid)]):
            return
        self.write({'wa_id': item.get('from'), 'last_inbound_at': fields.Datetime.now()})
        message_type = item.get('type')

        if message_type == 'interactive':
            reply = (item.get('interactive') or {}).get('button_reply') or {}
            return self._handle_button(msg_uid, reply.get('id') or '')
        if message_type == 'button':
            return self._handle_button(msg_uid, (item.get('button') or {}).get('payload') or '')
        if message_type == 'text':
            body = ((item.get('text') or {}).get('body') or '').strip()
            context = item.get('context') or {}
            if context.get('forwarded') or context.get('frequently_forwarded'):
                # Lo reenviado es material, nunca una orden ("ok" de un grupo no aprueba nada).
                request = self._collecting_request()
                request._add_inbound(msg_uid, 'text', body=body)
                request._bump()
                return
            return self._handle_text(msg_uid, body, context.get('id'))
        if message_type in SUPPORTED_MEDIA:
            media = item.get(message_type) or {}
            mime = media.get('mime_type') or ''
            kind = 'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else None
            if not kind:
                Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'other'})
                self._notify(_('Ese archivo no lo puedo usar: mandame fotos o videos.'))
                return
            request = self._collecting_request()
            attachment = self.env['auto.content.request']._fetch_inbound_attachment(account, msg_uid, media, request)
            request._add_inbound(msg_uid, kind, attachment=attachment, body=media.get('caption'))
            request._bump()
            return
        Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'other'})
        if message_type in ('audio', 'sticker', 'location', 'contacts'):
            self._notify(_('Por ahora solo uso fotos, videos y texto.'))

    def _handle_text(self, msg_uid, body, quoted_uid=None):
        Request = self.env['auto.content.request'].sudo()
        Message = self.env['auto.content.message'].sudo()
        if not body:
            Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'text'})
            return
        low = body.lower().strip(' .!¡¿?')

        if body.startswith('#') and body.lower() not in HELP_WORDS:
            code, rest = (body[1:].split(None, 1) + [''])[:2]
            brand = self.env['auto.content.brand'].sudo().search(
                [('code', '=', code.lower()), ('company_id', '=', self.company_id.id)], limit=1,
            )
            if not brand:
                Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'text', 'body': body})
                self._notify(_('No conozco la marca #%s.\n\n%s') % (code, self._help_text()))
                return
            self.brand_id = brand
            waiting = Request.search([('operator_id', '=', self.id), ('state', 'in', ('collecting', 'brand'))])
            waiting.write({'brand_id': brand.id})
            waiting.filtered(lambda r: r.state == 'brand').write({'state': 'queued'})
            self._notify(_('Marca activa: %s.') % brand.name)
            if not rest.strip():
                Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'text', 'body': body})
                return
            body, low = rest.strip(), rest.strip().lower()

        if low in HELP_WORDS:
            Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'text', 'body': body})
            self._notify(self._help_text())
            return

        waiting_change = Request.search([('operator_id', '=', self.id), ('state', '=', 'feedback')], limit=1)
        if not waiting_change and quoted_uid:
            version = self.env['auto.content.version'].sudo().search(
                [('wa_msg_uids', 'ilike', quoted_uid), ('request_id.operator_id', '=', self.id)], limit=1,
            )
            if version and version.request_id.state == 'review':
                waiting_change = version.request_id
        if waiting_change:
            waiting_change._add_inbound(msg_uid, 'text', body=body, counts_as_material=False)
            waiting_change._request_change(body)
            return

        in_review = Request.search([('operator_id', '=', self.id), ('state', '=', 'review')], limit=1)
        if in_review and low in APPROVE_WORDS:
            in_review._add_inbound(msg_uid, 'text', body=body, counts_as_material=False)
            in_review.current_version_id._approve()
            return
        if in_review and low in DISCARD_WORDS:
            in_review._add_inbound(msg_uid, 'text', body=body, counts_as_material=False)
            in_review._discard()
            return
        words = low.split()
        if words and words[0] in RETRY_WORDS and len(words) <= 4:
            failed = Request.search([('operator_id', '=', self.id), ('state', '=', 'error')], limit=1)
            Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'text', 'body': body})
            if failed:
                failed.write({'state': 'queued', 'error_message': False})
                self._notify(_('Reintento %s.') % failed.name)
            else:
                self._notify(_('No hay ninguna solicitud con error.'))
            return

        request = self._collecting_request()
        request._add_inbound(msg_uid, 'text', body=body)
        request._bump()

    def _handle_button(self, msg_uid, button_id):
        Message = self.env['auto.content.message'].sudo()
        parts = button_id.split(':')
        if len(parts) < 3 or parts[0] != 'ac':
            Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'button', 'body': button_id})
            return
        action = parts[1]
        if action == 'brand' and len(parts) == 4:
            request = self.env['auto.content.request'].sudo().browse(int(parts[2])).exists()
            brand = self.env['auto.content.brand'].sudo().browse(int(parts[3])).exists()
            Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'button', 'body': button_id,
                            'request_id': request.id if request else False})
            if request and brand and request.operator_id == self and request.state == 'brand':
                self.brand_id = brand
                request.write({'brand_id': brand.id, 'state': 'queued'})
                self._notify(_('Marca activa: %s. Armo %s.') % (brand.name, request.name))
            return

        version = self.env['auto.content.version'].sudo().browse(int(parts[2])).exists()
        request = version.request_id
        Message.create({'msg_uid': msg_uid, 'operator_id': self.id, 'kind': 'button', 'body': button_id,
                        'request_id': request.id if request else False})
        if not version or request.operator_id != self:
            return
        if version != request.current_version_id or request.state not in ('review', 'feedback'):
            self._notify(_('Esa propuesta ya no está pendiente (%s: %s).') % (
                request.name, dict(request._fields['state'].selection)[request.state]))
            return
        if action == 'approve':
            version._approve()
        elif action == 'change':
            request.state = 'feedback'
            self._notify(_('¿Qué cambio querés en %s? Escribilo en un mensaje.') % request.name)
        elif action == 'discard':
            request._discard()

    def _collecting_request(self):
        """La solicitud abierta del operador, o una nueva."""
        self.ensure_one()
        Request = self.env['auto.content.request'].sudo()
        request = Request.search([('operator_id', '=', self.id), ('state', '=', 'collecting')], limit=1)
        if request:
            return request
        brand = self.brand_id
        if not brand:
            brands = self.env['auto.content.brand'].sudo().search([('company_id', '=', self.company_id.id)])
            brand = brands if len(brands) == 1 else brand
        return Request.create({
            'operator_id': self.id,
            'brand_id': brand.id if brand else False,
            'company_id': self.company_id.id,
            'state': 'collecting',
            'process_after': fields.Datetime.now() + timedelta(seconds=self.debounce_seconds),
        })
