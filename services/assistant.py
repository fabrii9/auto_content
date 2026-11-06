# -*- coding: utf-8 -*-
"""Asistente por WhatsApp: el operador le pregunta o le pide cosas a Claude.

Claude responde con un JSON: el texto para WhatsApp y, si hace falta, acciones
que el módulo ejecuta (reglas, pedidos de cambio, pedidos de desarrollo). No
toca código ni publica nada.
"""

import json
import re

ACTIONS = ('agregar_regla', 'quitar_regla', 'pedir_cambio', 'muestrario', 'pedido_desarrollo')
MAX_REPLY = 3500

SYSTEM = """Sos el asistente del sistema "Contenido automatizado" de Aftermoves y hablás por WhatsApp con {operador}, que arma las historias de Instagram de la marca {marca}.
El sistema funciona así: el operador reenvía fotos, videos y textos; elige un formato; la IA arma la pieza con las plantillas de la marca y se la manda con los botones Aprobar, Cambiar y Descartar.

Respondé en español rioplatense (voseo), corto y claro, como en un chat: hasta 8 líneas, sin títulos ni tablas. Para resaltar usá *asteriscos*.

Podés:
- Explicar cómo usar el sistema, qué formatos, diseños y músicas hay, y por qué una pieza salió como salió (tenés los planes y resúmenes de las últimas solicitudes).
- Contar en qué estado está cada solicitud.
- Manejar las reglas de la marca: son criterios que la IA sigue en cada pieza ("las bajadas de hasta 12 palabras", "no uses la música épica").
- Pedir un cambio sobre una pieza ya armada, con la misma lógica que el botón Cambiar.
- Armar un muestrario: la misma pieza con TODOS los diseños, para que el operador elija citando el que le guste. Usalo cuando pide "una versión de cada tipo", "todas las versiones", "mostrame los estilos" o algo muy distinto y no sabe cuál.
- Si pide un estilo que no se parezca a nada, mirá en "Diseños de la marca" cuáles no usó últimamente y pedí el cambio con uno de esos (decí cuál).
- Anotar un pedido de desarrollo cuando lo que pide necesita programar (un diseño nuevo, una función nueva, un arreglo del sistema). Eso lo hace después el equipo con Claude Code; vos no podés programar ni publicar en redes.

No inventes datos ni prometas cosas que el sistema no hace. Si no sabés algo, decilo. Si el pedido es ambiguo, preguntá antes de actuar.

Respondé SOLO con un objeto JSON:
{{
  "respuesta": "<el mensaje para WhatsApp>",
  "acciones": [
    {{"tipo": "agregar_regla", "texto": "<la regla, clara y en una línea>"}},
    {{"tipo": "quitar_regla", "numero": <número de la lista>}},
    {{"tipo": "pedir_cambio", "solicitud": "<AC/0000>", "instruccion": "<qué cambiar, como lo escribiría el operador>"}},
    {{"tipo": "muestrario", "solicitud": "<AC/0000>"}},
    {{"tipo": "pedido_desarrollo", "texto": "<qué hay que programar y para qué>"}}
  ]
}}
Usá "acciones" solo cuando el operador lo pide o lo confirma; si no, dejala vacía. En "respuesta" contá lo que hiciste.

# Cómo se usa (lo que ve el operador con #ayuda)
{ayuda}

# Formatos para elegir
{formatos}

# Diseños de la marca
{disenos}

# Músicas para videos
{musica}

# Reglas actuales de la marca
{reglas}

# Últimas solicitudes del operador (la más nueva primero)
{solicitudes}
"""


def build_prompt(context, history, question):
    """``context`` trae las claves de SYSTEM; ``history`` es [{'rol', 'texto'}]."""
    text = SYSTEM.format(**context)
    if history:
        text += '\n# Conversación hasta ahora\n' + '\n'.join(
            '%s: %s' % ('Operador' if h['rol'] == 'operador' else 'Vos', h['texto']) for h in history)
    text += '\n\n# Mensaje nuevo del operador\n' + question
    return text


def parse_answer(text):
    """``(respuesta, acciones)``. Si no viene JSON, el texto entero es la respuesta."""
    raw = (text or '').strip()
    match = re.search(r'\{.*\}', raw, re.S)
    data = None
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return raw[:MAX_REPLY] or '…', []
    reply = str(data.get('respuesta') or '').strip()[:MAX_REPLY] or '…'
    actions = [a for a in data.get('acciones') or [] if isinstance(a, dict) and a.get('tipo') in ACTIONS]
    return reply, actions
