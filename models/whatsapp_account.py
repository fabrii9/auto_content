# -*- coding: utf-8 -*-

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class WhatsappAccount(models.Model):
    _inherit = 'whatsapp.account'

    def _process_messages(self, value):
        """Separa los mensajes de los operadores de contenido.

        Antes del flujo nativo se apaga el agente de ventas en su chat, para
        que no les conteste. Después, con el mensaje y sus archivos ya
        guardados por Odoo, se procesan como material o como respuesta.
        Un error acá nunca corta el webhook de WhatsApp.
        """
        data = value
        if 'messages' not in data and data.get('whatsapp_business_api_data', {}).get('messages'):
            data = data['whatsapp_business_api_data']
        Operator = self.env['auto.content.operator'].sudo()
        pending = []
        for item in data.get('messages', []):
            operator = Operator._match(self, item.get('from'))
            if operator:
                pending.append((operator, item))
        if pending:
            try:
                Operator.browse(list({op.id for op, _item in pending}))._mute_sales_agent()
            except Exception:
                _logger.exception('auto_content: no pude apagar el agente de ventas.')

        result = super()._process_messages(value)

        for operator, item in pending:
            try:
                with self.env.cr.savepoint():
                    operator._handle_inbound(self, item)
            except Exception:
                _logger.exception('auto_content: error procesando el mensaje %s.', item.get('id'))
        return result
