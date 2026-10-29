# -*- coding: utf-8 -*-
"""Prompt, mensajes multimodales y validación del plan que devuelve la IA.

La IA no diseña: elige un diseño del paquete de marca, el archivo a usar y
completa los campos de texto de la plantilla.
"""

import base64
import io
import json
import re

SYSTEM_PROMPT = """Sos el editor de contenidos para redes de la marca {nombre}.
Recibís material que mandó el equipo por WhatsApp (fotos, cuadros de video y textos reenviados) y lo convertís en UNA pieza de historia de Instagram usando uno de los diseños disponibles. No diseñás: completás los datos de una plantilla que ya está diseñada.

Reglas generales:
- Usá solo información que esté en el material recibido o en el conocimiento de la marca. Nunca inventes cifras, fechas, horarios, nombres ni lugares.
- Si falta un dato necesario o es dudoso, no lo pongas y avisalo en "alertas".
- Si el material menciona un lugar, normalizalo con la lista de lugares oficiales y usá el nombre oficial. Si no está en la lista, dejalo como viene y avisalo en "alertas".
- Elegí el mejor archivo: nítido, con el sujeto visible, sin marcas de agua y, si se puede, vertical.
- Mirá las imágenes y avisá en "alertas" si ves personas trabajando sin elementos de seguridad, caras de menores, patentes legibles, datos personales, mala calidad o cualquier cosa que no convenga publicar.
- Respondé solo con un objeto JSON válido, sin texto antes ni después, con esta forma:
{{
  "diseno": "<código de uno de los diseños disponibles>",
  "media": <número del archivo elegido (1, 2, ...) o null si el diseño no usa archivo>,
  "datos": {{ <los campos del diseño, sin "foto"> }},
  "lugar": "<lugar oficial detectado o null>",
  "resumen": "<una o dos frases para el operador: qué armaste y por qué>",
  "alertas": ["<avisos para el operador>"],
  "sticker": "<sticker de Instagram sugerido para sumar al publicar, o null>",
  "texto_publicacion": "<texto breve sugerido si se publica en el feed, o null>"
}}

# Diseños disponibles
{disenos}

# Lugares oficiales
{lugares}

# Instrucciones de la marca
{instrucciones}

# Conocimiento de la marca
{conocimiento}
"""

MEDIA_LABELS = {
    'requerida': 'necesita una foto o video',
    'opcional': 'puede llevar foto o video',
    'no': 'no usa foto ni video',
}


class PlanError(ValueError):
    """La respuesta de la IA no sirve."""


def describe_designs(designs):
    lines = []
    for code, spec in designs.items():
        line = '- `%s`: %s (%s).' % (code, spec.get('descripcion', ''), MEDIA_LABELS[spec.get('media', 'no')])
        if spec.get('campos'):
            line += ' Campos: %s' % spec['campos']
        lines.append(line)
    return '\n'.join(lines)


def describe_places(places):
    if not places:
        return '(sin lista de lugares)'
    lines = []
    for place in places:
        aliases = ', '.join(place.get('alias') or [])
        lines.append('- %s%s' % (place['nombre'], ' (también: %s)' % aliases if aliases else ''))
    return '\n'.join(lines)


def build_system_prompt(name, designs, places, instructions, knowledge, extra_instructions=''):
    instructions = (instructions or '').strip()
    if extra_instructions:
        instructions += '\n\n## Ajustes\n' + extra_instructions.strip()
    return SYSTEM_PROMPT.format(
        nombre=name,
        disenos=describe_designs(designs),
        lugares=describe_places(places),
        instrucciones=instructions or '(sin instrucciones adicionales)',
        conocimiento=(knowledge or '').strip() or '(sin datos adicionales)',
    )


def build_user_prompt(texts, media, previous_plan=None, instruction=None):
    """``media`` es una lista de dicts con ``index`` y ``kind`` (image/video)."""
    parts = ['## Textos recibidos']
    parts.append('\n'.join('- %s' % t for t in texts if t.strip()) or '(no mandaron texto)')
    parts.append('\n## Archivos recibidos')
    if media:
        for item in media:
            label = 'foto' if item['kind'] == 'image' else 'video (te muestro un cuadro)'
            parts.append('%d. %s' % (item['index'], label))
        parts.append('Las imágenes van en el mismo orden.')
    else:
        parts.append('(no mandaron archivos: usá un diseño que no necesite foto)')
    if previous_plan:
        parts.append('\n## Versión anterior')
        parts.append(json.dumps(previous_plan, ensure_ascii=False))
        parts.append('\n## Cambio que pide el operador')
        parts.append(instruction or 'Hacé otra versión distinta de la anterior.')
        parts.append('Aplicá el cambio y mantené lo demás salvo que el pedido diga otra cosa.')
    return '\n'.join(parts)


def to_jpeg(data, max_side=1280):
    """Achica la imagen para mandarla a la IA (menos tokens, mismo criterio)."""
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image).convert('RGB')
    image.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=85)
    return buffer.getvalue()


def build_messages(provider_type, system_text, user_text, images):
    """Arma los mensajes en el formato de cada proveedor. ``images`` son JPEG."""
    if provider_type == 'anthropic':
        content = [
            {'type': 'image', 'source': {
                'type': 'base64', 'media_type': 'image/jpeg',
                'data': base64.b64encode(image).decode()}}
            for image in images
        ]
        content.append({'type': 'text', 'text': user_text})
    elif provider_type == 'gemini':
        raise PlanError('LLM Connector todavía no manda imágenes a Gemini: usá OpenAI o Anthropic.')
    else:
        content = [{'type': 'text', 'text': user_text}]
        content += [
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/jpeg;base64,%s' % base64.b64encode(image).decode()}}
            for image in images
        ]
    return [
        {'role': 'system', 'content': system_text},
        {'role': 'user', 'content': content},
    ]


def _extract_json(text):
    text = (text or '').strip()
    fenced = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise PlanError('La IA no devolvió JSON.')
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        raise PlanError('La IA devolvió JSON inválido: %s' % exc) from exc


def _clean_text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_plan(text, designs, media):
    """Valida y normaliza el plan. ``media`` como en ``build_user_prompt``."""
    plan = _extract_json(text)
    design = plan.get('diseno')
    if design not in designs:
        raise PlanError('La IA eligió un diseño que no existe: %r.' % design)
    mode = designs[design].get('media', 'no')

    index = plan.get('media')
    valid = {item['index'] for item in media}
    if isinstance(index, str) and index.isdigit():
        index = int(index)
    if index not in valid:
        index = None
    if mode == 'requerida' and index is None:
        if not media:
            raise PlanError('El diseño %s necesita una foto o video y no hay archivos.' % design)
        index = media[0]['index']
    if mode == 'no':
        index = None

    data = plan.get('datos')
    if not isinstance(data, dict):
        raise PlanError('Faltan los "datos" del diseño.')
    for reserved in ('foto', 'transparente', 'plantilla'):
        data.pop(reserved, None)

    alerts = plan.get('alertas') or []
    if isinstance(alerts, str):
        alerts = [alerts]
    return {
        'diseno': design,
        'media': index,
        'datos': data,
        'lugar': _clean_text(plan.get('lugar')),
        'resumen': _clean_text(plan.get('resumen')) or '',
        'alertas': [a.strip() for a in alerts if isinstance(a, str) and a.strip()],
        'sticker': _clean_text(plan.get('sticker')),
        'texto_publicacion': _clean_text(plan.get('texto_publicacion')),
    }
