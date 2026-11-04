# -*- coding: utf-8 -*-
"""Claude Code corriendo en el mismo servidor que Odoo (modo "en el servidor").

Es lo mismo que hace ``tools/claude_worker.py`` en una PC, pero desde la tarea
automática: escribe las imágenes en un directorio, ejecuta ``claude -p`` con
permiso solo para leerlas y devuelve el texto de la respuesta.

La sesión es la suscripción del operador: Claude Code toma el token de larga
duración de ``CLAUDE_CODE_OAUTH_TOKEN`` (generado con ``claude setup-token``),
que el contenedor recibe como variable de entorno.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from .renderer import _CHILD

CANDIDATES = ('/usr/local/bin/claude', '/usr/bin/claude')


class ClaudeError(RuntimeError):
    """Claude Code no pudo responder."""


def find_claude(override=None):
    for path in ((override,) if override else CANDIDATES) + (shutil.which('claude') or '',):
        if path and os.access(path, os.X_OK):
            return path
    raise ClaudeError('No encontré Claude Code en el servidor (ver README: modo "en el servidor").')


def build_prompt(system_text, user_text, image_names):
    images = '\n'.join('- %s (imagen %d)' % (name, i) for i, name in enumerate(image_names, 1))
    return (
        system_text
        + '\n\n# Material de esta solicitud\n'
        + user_text
        + '\n\n# Imágenes\nEstán en el directorio actual. Leé cada una con la herramienta Read antes de decidir:\n'
        + (images or '(no hay imágenes)')
        + '\n\nRespondé solo con el objeto JSON pedido, sin texto antes ni después.'
    )


def _environment(home):
    env = dict(os.environ)
    if not env.get('CLAUDE_CODE_OAUTH_TOKEN') and not env.get('ANTHROPIC_API_KEY'):
        raise ClaudeError('Falta CLAUDE_CODE_OAUTH_TOKEN en el contenedor (se genera con "claude setup-token").')
    Path(home).mkdir(parents=True, exist_ok=True)
    env.update({'HOME': str(home), 'DISABLE_AUTOUPDATER': '1'})
    return env


def run(claude, model, prompt, workdir, home, timeout=600, max_turns=30):
    """Ejecuta ``claude -p`` en ``workdir`` (solo puede leer) y devuelve el texto."""
    command = [
        claude, '-p', '--output-format', 'json', '--model', model,
        '--tools', 'Read', '--allowedTools', 'Read',
        '--max-turns', str(max_turns), '--no-session-persistence',
    ]
    try:
        result = subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=workdir, timeout=timeout, env=_environment(home), **_CHILD,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError('Claude Code tardó más de %s s.' % timeout) from exc
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise ClaudeError('Claude Code no respondió JSON (código %s): %s' % (
            result.returncode, (result.stderr or result.stdout or 'sin mensaje')[-500:]))
    if data.get('is_error'):
        raise ClaudeError('Claude Code: %s' % (data.get('result') or data.get('subtype') or 'error'))
    return data.get('result') or ''
