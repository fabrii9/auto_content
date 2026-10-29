# -*- coding: utf-8 -*-
"""Envíos al operador por la API de WhatsApp de la cuenta de Odoo.

Usa la ``WhatsAppApi`` del módulo enterprise. Su ``_send_whatsapp`` no
admite mensajes ``interactive``: los botones se mandan con su método de
request interno, con la misma autenticación y manejo de errores.
"""

import json

from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi
from odoo.addons.whatsapp.tools.whatsapp_exception import WhatsAppError

# Límites de la Cloud API
MAX_VIDEO_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_BUTTON_TITLE = 20
MAX_BODY = 1024


class WhatsAppSender:

    def __init__(self, account, number):
        self.api = WhatsAppApi(account)
        self.number = number

    def text(self, body):
        return self.api._send_whatsapp(self.number, 'text', {'body': body[:4096], 'preview_url': False})

    def media(self, attachment, kind, caption=None, filename=None):
        media_id = self.api._upload_whatsapp_document(attachment)
        vals = {'id': media_id}
        if caption:
            vals['caption'] = caption[:MAX_BODY]
        if kind == 'document':
            vals['filename'] = filename or attachment.name
        return self.api._send_whatsapp(self.number, kind, vals)

    def buttons(self, body, buttons):
        """``buttons`` es una lista de hasta 3 tuplas ``(id, título)``."""
        data = {
            'messaging_product': 'whatsapp',
            'recipient_type': 'individual',
            'to': self.number,
            'type': 'interactive',
            'interactive': {
                'type': 'button',
                'body': {'text': body[:MAX_BODY]},
                'action': {'buttons': [
                    {'type': 'reply', 'reply': {'id': button_id[:256], 'title': title[:MAX_BUTTON_TITLE]}}
                    for button_id, title in buttons[:3]
                ]},
            },
        }
        response = self.api._WhatsAppApi__api_requests(
            'POST', '/%s/messages' % self.api.phone_uid, auth_type='bearer',
            headers={'Content-Type': 'application/json'}, data=json.dumps(data),
        )
        response_json = response.json()
        if response_json.get('messages'):
            return response_json['messages'][0]['id']
        raise WhatsAppError(*self.api._prepare_error_response(response_json))
