# -*- coding: utf-8 -*-
"""Render de piezas con Chromium headless y composición de video con ffmpeg.

La plantilla HTML del paquete recibe los datos como JSON entre ``/*DATOS*/``
y ``/*FIN*/``. Para videos se renderiza la plantilla con ``"transparente":
true`` y sin foto, con fondo transparente, y ffmpeg la superpone al video.
"""

import glob
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

CHROME_CANDIDATES = (
    '/opt/ms-playwright/chromium-*/chrome-linux*/chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
)
MAX_VIDEO_SECONDS = 60


class RenderError(RuntimeError):
    """No se pudo generar la pieza."""


def _lift_memory_limit():
    """Los workers de Odoo limitan la memoria virtual (limit_memory_hard) y los
    procesos hijos lo heredan; Chromium reserva mucho más al arrancar y muere.
    Se levanta solo en el proceso hijo, hasta el límite duro del sistema."""
    try:
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
    except (ImportError, ValueError, OSError):
        pass


_CHILD = {'preexec_fn': _lift_memory_limit} if os.name == 'posix' else {}


def find_chrome(override=None):
    candidates = (override,) if override else CHROME_CANDIDATES
    for pattern in candidates:
        for path in sorted(glob.glob(pattern), reverse=True):
            if os.access(path, os.X_OK):
                return path
    raise RenderError(
        'No encontré Chromium. Instalalo en el contenedor (ver README) o '
        'configurá el parámetro auto_content.chrome_path.'
    )


def find_ffmpeg(override=None):
    path = override or shutil.which('ffmpeg')
    if not path:
        raise RenderError('No encontré ffmpeg. Instalalo en el contenedor (ver README).')
    return path


def inject_data(template_html, data):
    start, end = template_html.index('/*DATOS*/'), template_html.index('/*FIN*/')
    # "</" dentro de un <script> cerraría la etiqueta: se escapa.
    payload = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    return template_html[:start] + payload + template_html[end + len('/*FIN*/'):]


def render_html(template_path, data, out_path, width, height, chrome, transparent=False, timeout=90):
    """Renderiza la plantilla con ``data`` y guarda un PNG en ``out_path``."""
    template_path = Path(template_path)
    html = inject_data(template_path.read_text(encoding='utf-8'), data)
    # El temporal va junto a la plantilla para que funcionen sus rutas relativas.
    page = template_path.parent / ('.render-%s.html' % uuid.uuid4().hex)
    page.write_text(html, encoding='utf-8')
    profile = tempfile.mkdtemp(prefix='chrome-')
    command = [
        chrome, '--headless=new', '--no-sandbox', '--disable-gpu',
        '--disable-dev-shm-usage', '--hide-scrollbars', '--no-first-run',
        '--allow-file-access-from-files', '--force-device-scale-factor=1',
        '--user-data-dir=%s' % profile,
        '--window-size=%d,%d' % (width, height),
        '--virtual-time-budget=5000',
        '--screenshot=%s' % out_path,
    ]
    if transparent:
        command.append('--default-background-color=00000000')
    command.append(page.as_uri())
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=timeout,
            env={**os.environ, 'HOME': profile}, **_CHILD,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError('Chromium tardó más de %s s.' % timeout) from exc
    finally:
        page.unlink(missing_ok=True)
        shutil.rmtree(profile, ignore_errors=True)
    if result.returncode != 0 or not Path(out_path).exists():
        raise RenderError('Chromium falló (código %s): %s' % (
            result.returncode, result.stderr.decode('utf-8', 'replace')[-800:] or 'sin mensaje'))
    return Path(out_path)


def _run_ffmpeg(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, **_CHILD)
    except subprocess.TimeoutExpired as exc:
        raise RenderError('ffmpeg tardó más de %s s.' % timeout) from exc
    if result.returncode != 0:
        raise RenderError('ffmpeg falló: %s' % result.stderr.decode('utf-8', 'replace')[-800:])


def video_frame(video_path, out_path, ffmpeg, second=1.0):
    """Saca un cuadro del video (para mostrárselo a la IA)."""
    for at in (second, 0):
        command = [ffmpeg, '-y', '-loglevel', 'error', '-ss', str(at), '-i', str(video_path),
                   '-frames:v', '1', '-q:v', '3', str(out_path)]
        try:
            _run_ffmpeg(command, timeout=60)
        except RenderError:
            continue
        if Path(out_path).exists() and Path(out_path).stat().st_size:
            return Path(out_path)
    raise RenderError('No pude sacar un cuadro del video.')


def compose_video(video_path, overlay_png, out_path, width, height, ffmpeg,
                  max_seconds=MAX_VIDEO_SECONDS, timeout=480):
    """Recorta el video a ``width``×``height``, le superpone el PNG y lo
    corta a ``max_seconds``. Sale en MP4 H.264 + AAC, listo para historias."""
    graph = (
        '[0:v]scale=%(w)d:%(h)d:force_original_aspect_ratio=increase,'
        'crop=%(w)d:%(h)d,setsar=1[fondo];'
        '[fondo][1:v]overlay=0:0,format=yuv420p[v]'
    ) % {'w': width, 'h': height}
    command = [
        ffmpeg, '-y', '-loglevel', 'error', '-i', str(video_path), '-i', str(overlay_png),
        '-filter_complex', graph, '-map', '[v]', '-map', '0:a?',
        '-t', str(max_seconds), '-r', '30',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(out_path),
    ]
    _run_ffmpeg(command, timeout=timeout)
    return Path(out_path)
