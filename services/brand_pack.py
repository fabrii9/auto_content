# -*- coding: utf-8 -*-
"""Lectura y extracción de paquetes de marca (.zip).

Un paquete tiene en la raíz un ``marca.json`` como este::

    {
      "nombre": "CAPSIC",
      "codigo": "capsic",
      "plantilla": "plantillas/historia.html",
      "ancho": 1080, "alto": 1920,
      "instrucciones": "instrucciones_ia.md",
      "conocimiento": ["conocimiento.md"],
      "foto_prueba": "assets/fotos/planta-3051.jpg",
      "texto_prueba": "Cambio de postes en Lomas Este",
      "disenos": {"foto": {"descripcion": "...", "media": "requerida", "campos": "..."}},
      "lugares": [{"nombre": "Colonia Progreso", "alias": ["Col. Progreso"]}]
    }

La plantilla HTML recibe los datos entre los marcadores ``/*DATOS*/`` y
``/*FIN*/`` (ver ``renderer.py``).
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .video_edit import TRANSITIONS

MANIFEST = 'marca.json'
MAX_PACK_BYTES = 60 * 1024 * 1024
MEDIA_MODES = ('requerida', 'opcional', 'no')


class PackError(ValueError):
    """Paquete de marca inválido."""


def _check_names(names):
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or name.startswith('\\'):
            raise PackError('El paquete tiene una ruta no permitida: %s' % name)


def _read_text(zf, names, path):
    if path not in names:
        raise PackError('marca.json menciona %s, pero no está en el paquete.' % path)
    return zf.read(path).decode('utf-8')


def read_pack(data):
    """Valida el .zip y devuelve ``(manifest, textos, checksum)``.

    ``textos`` tiene las claves ``instrucciones`` y ``conocimiento`` ya leídas.
    """
    if not data:
        raise PackError('El paquete está vacío.')
    if len(data) > MAX_PACK_BYTES:
        raise PackError('El paquete supera los 60 MB.')
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackError('El archivo no es un .zip válido.') from exc
    with zf:
        names = set(zf.namelist())
        _check_names(names)
        if MANIFEST not in names:
            raise PackError('Falta marca.json en la raíz del paquete.')
        try:
            manifest = json.loads(zf.read(MANIFEST).decode('utf-8'))
        except ValueError as exc:
            raise PackError('marca.json no es JSON válido: %s' % exc) from exc
        if not isinstance(manifest, dict):
            raise PackError('marca.json tiene que ser un objeto.')

        for key in ('nombre', 'codigo', 'plantilla'):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                raise PackError('Falta "%s" en marca.json.' % key)
        if manifest['plantilla'] not in names:
            raise PackError('No está la plantilla %s en el paquete.' % manifest['plantilla'])
        template = zf.read(manifest['plantilla']).decode('utf-8')
        if '/*DATOS*/' not in template or '/*FIN*/' not in template:
            raise PackError('La plantilla tiene que tener los marcadores /*DATOS*/ y /*FIN*/.')

        for key in ('ancho', 'alto'):
            value = manifest.get(key)
            if not isinstance(value, int) or not 200 <= value <= 4000:
                raise PackError('"%s" tiene que ser un entero entre 200 y 4000.' % key)

        designs = manifest.get('disenos')
        if not isinstance(designs, dict) or not designs:
            raise PackError('marca.json tiene que definir al menos un diseño en "disenos".')
        for code, spec in designs.items():
            if not isinstance(spec, dict):
                raise PackError('El diseño %s tiene que ser un objeto.' % code)
            if spec.get('media', 'no') not in MEDIA_MODES:
                raise PackError('El diseño %s tiene "media" inválido (usar %s).' % (code, ', '.join(MEDIA_MODES)))

        places = manifest.get('lugares', [])
        if not isinstance(places, list) or any(
            not isinstance(p, dict) or not isinstance(p.get('nombre'), str) for p in places
        ):
            raise PackError('"lugares" tiene que ser una lista de objetos con "nombre".')

        texts = {'instrucciones': '', 'conocimiento': ''}
        if manifest.get('instrucciones'):
            texts['instrucciones'] = _read_text(zf, names, manifest['instrucciones'])
        knowledge = manifest.get('conocimiento') or []
        if isinstance(knowledge, str):
            knowledge = [knowledge]
        texts['conocimiento'] = '\n\n'.join(_read_text(zf, names, path) for path in knowledge)

        _check_video(manifest.get('video'), names)

        for key in ('foto_prueba',):
            if manifest.get(key) and manifest[key] not in names:
                raise PackError('No está %s (%s) en el paquete.' % (manifest[key], key))

    return manifest, texts, hashlib.sha256(data).hexdigest()


def _check_video(video, names):
    """Valida la sección opcional "video" (montajes)."""
    if video is None:
        return
    if not isinstance(video, dict):
        raise PackError('"video" tiene que ser un objeto.')
    limit = video.get('duracion_max', 60)
    if not isinstance(limit, (int, float)) or not 5 <= limit <= 180:
        raise PackError('"video.duracion_max" tiene que estar entre 5 y 180 segundos.')
    unknown = [t for t in video.get('transiciones') or [] if t not in TRANSITIONS]
    if unknown:
        raise PackError('Transiciones desconocidas en "video": %s (se puede usar %s).' % (
            ', '.join(unknown), ', '.join(TRANSITIONS)))
    for track in video.get('musica') or []:
        if not isinstance(track, dict) or not track.get('id') or track.get('archivo') not in names:
            raise PackError('Cada pista de "video.musica" necesita "id" y un "archivo" que esté en el paquete.')
    font = (video.get('subtitulos') or {}).get('fuente')
    if font and font not in names:
        raise PackError('No está la fuente de subtítulos %s en el paquete.' % font)


def extract_pack(data, checksum, cache_key):
    """Extrae el paquete en un directorio de caché y devuelve su ``Path``.

    Si ya estaba extraído con el mismo checksum, lo reutiliza. La caché vive
    en el temporal del sistema: si se borra, se vuelve a extraer.
    """
    root = Path(tempfile.gettempdir()) / 'odoo_auto_content'
    target = root / ('%s-%s' % (cache_key, checksum[:16]))
    marker = target / '.ok'
    if marker.exists():
        return target
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='pack-', dir=root))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            _check_names(zf.namelist())
            zf.extractall(work)
        (work / '.ok').write_text(checksum)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        os.replace(work, target)
    except FileExistsError:
        # Otro worker lo extrajo al mismo tiempo.
        shutil.rmtree(work, ignore_errors=True)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return target
