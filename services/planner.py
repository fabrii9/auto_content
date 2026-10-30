# -*- coding: utf-8 -*-
"""Prompt, mensajes multimodales y validación del plan que devuelve la IA.

La IA no diseña: elige un diseño del paquete de marca, el archivo a usar y
completa los campos de texto de la plantilla.
"""

import base64
import io
import json
import re

from .video_edit import TRANSITIONS, frames, timeline

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

MONTAGE_PROMPT = """
# Montaje de video
Esta vez llegaron videos: armá UN video editado para historia de Instagram (vertical, {ancho}×{alto}) con las mejores partes del material. No editás vos: escribís el guion y el sistema lo arma.

Cómo ves los videos: por cada video recibís una o más hojas de contacto (cuadros repartidos en todo el video, con su segundo escrito arriba a la izquierda), los segundos donde cambia la toma y, si se pudo, lo que se dice con sus tiempos. Usá esos segundos para elegir "desde" y "hasta".

Además de las claves de siempre, respondé con la clave "montaje":
"montaje": {{
  "clips": [
    {{"video": <número de archivo>, "desde": <segundo>, "hasta": <segundo>, "transicion": "<cómo entra este clip>", "velocidad": 1, "estabilizar": false, "encuadre": 0.5, "rotulo": "<texto corto o null>"}},
    {{"foto": <número de archivo>, "duracion": 3, "transicion": "fundido", "rotulo": null}}
  ],
  "audio": "<original | mezcla | musica>",
  "musica": "<id de la pista o null>",
  "subtitulos": <true si alguien habla y conviene subtitular>,
  "capa": "<inicio | todo | ninguna>",
  "cierre": {{"diseno": "<diseño sin foto, por ejemplo cierre>", "datos": {{...}}, "segundos": 2.5}} o null
}}

Reglas del montaje:
- Clips en el orden en que se ven. Duración total máxima: {duracion_max} s (las transiciones se superponen {transicion} s). Lo normal es 15 a 40 s, con clips de 2 a 5 s.
- "transicion" es cómo entra cada clip (en el primero no se usa): {transiciones}. Usá "corte" para el ritmo y las otras con moderación.
- "velocidad" entre 0.5 (cámara lenta) y 2 (rápido). "estabilizar": true solo si la toma se mueve mucho. "encuadre" (0 a 1) corre el recorte de una toma horizontal: 0 izquierda, 0.5 centro, 1 derecha; "completo" muestra la toma horizontal entera sobre un fondo desenfocado (el sistema lo usa solo si la toma tiene poca resolución).
- "rotulo": texto corto (hasta 40 caracteres) que aparece abajo durante ese clip. Solo datos del material; null si no hace falta.
- "audio": "original" deja el sonido de los videos; "mezcla" suma música baja que se corre cuando alguien habla; "musica" tapa el sonido original con la música (para tomas con ruido sin interés).
- Si nadie habla, poné música: "mezcla" si el sonido ambiente suma (máquinas, obra) o "musica" si es ruido. Elegí la pista según el ánimo del material.
- "capa": la capa de marca con "diseno" y "datos" (un diseño que lleve foto o video, por ejemplo foto) sobre el video: "inicio" los primeros segundos, "todo" durante todo el video, "ninguna" sin capa. Mientras se ve la capa no se ven los subtítulos.
- "cierre": una placa final opcional con un diseño que no use foto.
- No cortes a una persona a mitad de frase si se escucha su voz. Si un video entero está mal (movido, oscuro, sin interés), no lo uses y avisalo en "alertas".

Pistas de música disponibles:
{musica}
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


def describe_music(tracks):
    lines = ['- `%s` (%s): %s' % (t['id'], t.get('animo') or '', t.get('descripcion') or '') for t in tracks or []]
    return '\n'.join(lines) or '(no hay música: usá "audio": "original")'


def build_system_prompt(name, designs, places, instructions, knowledge, extra_instructions='', video=None):
    """``video`` es la sección "video" del paquete y se pasa solo si hay que montar."""
    instructions = (instructions or '').strip()
    if extra_instructions:
        instructions += '\n\n## Ajustes\n' + extra_instructions.strip()
    text = SYSTEM_PROMPT.format(
        nombre=name,
        disenos=describe_designs(designs),
        lugares=describe_places(places),
        instrucciones=instructions or '(sin instrucciones adicionales)',
        conocimiento=(knowledge or '').strip() or '(sin datos adicionales)',
    )
    if video:
        text += MONTAGE_PROMPT.format(
            ancho=video.get('ancho', 1080), alto=video.get('alto', 1920),
            duracion_max=video.get('duracion_max', 60), transicion=video.get('transicion_segundos', 0.4),
            transiciones=', '.join('"%s"' % t for t in video.get('transiciones') or ['corte', 'fundido']),
            musica=describe_music(video.get('musica')),
        )
    return text


def build_user_prompt(texts, media, previous_plan=None, instruction=None, extra=None):
    """``media`` es una lista de dicts con ``index`` y ``kind`` (image/video);
    con ``detalle`` describe el archivo (y qué imágenes le corresponden).
    ``extra`` se agrega tal cual (el análisis de los videos)."""
    parts = ['## Textos recibidos']
    parts.append('\n'.join('- %s' % t for t in texts if t.strip()) or '(no mandaron texto)')
    parts.append('\n## Archivos recibidos')
    if media:
        for item in media:
            label = item.get('detalle') or ('foto' if item['kind'] == 'image' else 'video (te muestro un cuadro)')
            parts.append('%d. %s' % (item['index'], label))
        if not any(item.get('detalle') for item in media):
            parts.append('Las imágenes van en el mismo orden.')
    else:
        parts.append('(no mandaron archivos: usá un diseño que no necesite foto)')
    if extra:
        parts.append('\n' + extra.strip())
    if previous_plan:
        parts.append('\n## Versión anterior')
        parts.append(json.dumps(previous_plan, ensure_ascii=False))
        parts.append('\n## Cambio que pide el operador')
        parts.append(instruction or 'Hacé otra versión distinta de la anterior.')
        parts.append('Aplicá el cambio y mantené lo demás salvo que el pedido diga otra cosa.')
    return '\n'.join(parts)


def describe_transcripts(transcripts):
    """Lo que se dice en cada video, con tiempos (``None`` si no hay)."""
    blocks = []
    for key in sorted(transcripts or {}, key=lambda k: int(k) if str(k).isdigit() else 0):
        segments = (transcripts[key] or {}).get('segmentos') or []
        lines = ['[%.1f–%.1f] %s' % (s['inicio'], s['fin'], (s.get('texto') or '').strip())
                 for s in segments if (s.get('texto') or '').strip()]
        blocks.append('Video %s:\n%s' % (key, '\n'.join(lines) or '(no se entiende nada que se diga)'))
    if not blocks:
        return None
    return '## Lo que se dice en los videos (segundos del video original)\n' + '\n\n'.join(blocks)


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


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _index(value):
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_montage(raw, designs, montage):
    """Valida el guion del montaje.

    ``montage`` trae ``cfg`` (la sección "video" del paquete), ``videos``
    (número de archivo → ``{'duracion', ...}``) y ``fotos`` (números de
    archivo de las fotos). Las duraciones quedan en cuadros enteros.
    """
    if not isinstance(raw, dict):
        raise PlanError('Falta "montaje": llegaron videos y hay que devolver el guion del montaje.')
    cfg = montage['cfg']
    videos = {int(k): v for k, v in montage['videos'].items()}
    photos = set(montage.get('fotos') or [])
    allowed = [t for t in cfg.get('transiciones') or ['corte', 'fundido'] if t in TRANSITIONS]
    fallback = 'fundido' if 'fundido' in allowed else 'corte'
    clips = []
    for number, item in enumerate(raw.get('clips') or [], 1):
        if not isinstance(item, dict):
            raise PlanError('El clip %d no es un objeto.' % number)
        transition = item.get('transicion') or 'corte'
        if transition not in allowed:
            transition = fallback if transition in TRANSITIONS else 'corte'
        label = _clean_text(item.get('rotulo'))
        clip = {
            'transicion': transition,
            'rotulo': label[:60] if label else None,
            'velocidad': 1.0,
            'estabilizar': False,
            'encuadre': 0.5,
        }
        if item.get('video') is not None:
            index = _index(item.get('video'))
            if index not in videos:
                raise PlanError('El clip %d usa el video %r, que no existe (videos: %s).' % (
                    number, item.get('video'), ', '.join(map(str, sorted(videos)))))
            length = videos[index]['duracion']
            start = max(0.0, _number(item.get('desde'), 0.0))
            end = min(length, _number(item.get('hasta'), length))
            if end - start < 0.5:
                raise PlanError('El clip %d (video %d) dura menos de medio segundo: revisá "desde" y "hasta" '
                                '(el video dura %.1f s).' % (number, index, length))
            speed = min(2.0, max(0.5, _number(item.get('velocidad'), 1.0)))
            clip.update({
                'tipo': 'video', 'archivo': index, 'desde': round(start, 2), 'hasta': round(end, 2),
                'duracion': frames((end - start) / speed), 'velocidad': speed,
                'estabilizar': bool(item.get('estabilizar')),
                'encuadre': 'completo' if item.get('encuadre') == 'completo'
                else min(1.0, max(0.0, _number(item.get('encuadre'), 0.5))),
            })
        elif item.get('foto') is not None:
            index = _index(item.get('foto'))
            if index not in photos:
                raise PlanError('El clip %d usa la foto %r, que no existe.' % (number, item.get('foto')))
            length = frames(min(6.0, max(1.5, _number(item.get('duracion'), 3.0))))
            clip.update({'tipo': 'foto', 'archivo': index, 'desde': 0.0, 'hasta': length, 'duracion': length})
        else:
            raise PlanError('El clip %d necesita "video" o "foto".' % number)
        clips.append(clip)
    if not clips:
        raise PlanError('El montaje no tiene clips.')
    if len(clips) > 20:
        raise PlanError('El montaje tiene %d clips; el máximo es 20.' % len(clips))

    closing = raw.get('cierre')
    if closing:
        if not isinstance(closing, dict) or closing.get('diseno') not in designs:
            raise PlanError('El "cierre" tiene que usar un diseño que exista.')
        if designs[closing['diseno']].get('media', 'no') == 'requerida':
            raise PlanError('El cierre %s necesita foto: usá un diseño sin foto.' % closing['diseno'])
        data = closing.get('datos') if isinstance(closing.get('datos'), dict) else {}
        for reserved in ('foto', 'transparente', 'plantilla'):
            data.pop(reserved, None)
        length = frames(min(5.0, max(1.5, _number(closing.get('segundos'), 2.5))))
        closing = {'diseno': closing['diseno'], 'datos': data, 'segundos': length}
    else:
        closing = None

    limit = float(cfg.get('duracion_max') or 60)
    sequence = clips + ([{'duracion': closing['segundos'], 'transicion': fallback}] if closing else [])
    _starts, total = timeline(sequence, float(cfg.get('transicion_segundos') or 0.4))
    if total > limit + 0.5:
        raise PlanError('El montaje dura %.1f s y el máximo es %d s: acortá o sacá clips.' % (total, limit))

    tracks = {t['id'] for t in cfg.get('musica') or []}
    music = raw.get('musica') if raw.get('musica') in tracks else None
    audio = raw.get('audio') if raw.get('audio') in ('original', 'mezcla', 'musica') else 'original'
    if audio != 'original' and not music:
        audio = 'original'
    layer = raw.get('capa') if raw.get('capa') in ('inicio', 'todo', 'ninguna') else 'inicio'
    return {
        'clips': clips,
        'cierre': closing,
        'audio': audio,
        'musica': music,
        'subtitulos': bool(raw.get('subtitulos')),
        'capa': layer,
        'duracion': total,
    }


def parse_plan(text, designs, media, montage=None):
    """Valida y normaliza el plan. ``media`` como en ``build_user_prompt``.

    Con ``montage`` (ver ``parse_montage``) el plan tiene que traer el guion
    del montaje y el diseño es la capa de marca sobre el video.
    """
    plan = _extract_json(text)
    design = plan.get('diseno')
    if design not in designs:
        raise PlanError('La IA eligió un diseño que no existe: %r.' % design)
    mode = designs[design].get('media', 'no')
    edit = parse_montage(plan.get('montaje'), designs, montage) if montage else None
    if edit and edit['capa'] != 'ninguna' and mode == 'no':
        raise PlanError('Para la capa sobre el video usá un diseño que lleve foto o video (o "capa": "ninguna").')
    if edit:
        mode = 'no'

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
    result = {
        'diseno': design,
        'media': index,
        'datos': data,
        'lugar': _clean_text(plan.get('lugar')),
        'resumen': _clean_text(plan.get('resumen')) or '',
        'alertas': [a.strip() for a in alerts if isinstance(a, str) and a.strip()],
        'sticker': _clean_text(plan.get('sticker')),
        'texto_publicacion': _clean_text(plan.get('texto_publicacion')),
    }
    if edit:
        result['montaje'] = edit
    return result
