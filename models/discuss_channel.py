# -*- coding: utf-8 -*-

from odoo import api, models

from .auto_content_operator import phone_key


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    @api.model_create_multi
    def create(self, vals_list):
        """El chat de WhatsApp de un operador nace con el agente de ventas apagado.

        Si no, el agente de whatsapp_ai_agents podría contestarle el primer
        mensaje antes de que el módulo lo silencie.
        """
        if 'ai_agent_enabled' in self._fields:
            keys = set(self.env['auto.content.operator'].sudo().search([]).mapped('phone_key'))
            for vals in vals_list:
                number = vals.get('whatsapp_number')
                if vals.get('channel_type') == 'whatsapp' and number and phone_key(number) in keys:
                    vals.update(ai_agent_enabled=False, ai_agent_disabled_date=False)
        return super().create(vals_list)
