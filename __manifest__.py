{
    'name': 'Contenido automatizado',
    'version': '19.0.1.1.0',
    'category': 'Marketing',
    'summary': 'Convierte fotos y videos recibidos por WhatsApp en piezas de marca, con aprobación por WhatsApp',
    'description': """
Contenido automatizado
======================

Un operador reenvía fotos, videos y textos al número de WhatsApp de Odoo.
El módulo junta el material, lo analiza con IA usando el paquete de marca
del cliente (plantilla, voz, datos y lugares), arma la pieza y se la devuelve
al operador por WhatsApp para aprobarla, pedir cambios o descartarla.

Es multi-marca: cada cliente es un paquete de marca (.zip) y no hay nada
de un cliente concreto en el código.
    """,
    'author': 'Aftermoves',
    'license': 'LGPL-3',
    'depends': [
        'mail',
        'whatsapp',
        'llm_connector',
    ],
    'data': [
        'security/auto_content_security.xml',
        'security/ir.model.access.csv',
        'data/sequence_data.xml',
        'data/cron_data.xml',
        'views/auto_content_request_views.xml',
        'views/auto_content_brand_views.xml',
        'views/auto_content_operator_views.xml',
        'views/auto_content_menus.xml',
    ],
    'application': True,
    'installable': True,
}
