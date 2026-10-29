#!/usr/bin/env python3
"""Arma el .zip de un paquete de marca a partir de una carpeta.

Uso:
    python3 armar_paquete.py /ruta/a/marca            # → marca/<codigo>-paquete.zip
    python3 armar_paquete.py /ruta/a/marca -o x.zip

La carpeta tiene que tener un marca.json. Se incluye lo que liste
"incluir" (archivos o carpetas); si no está, marca.json, la plantilla,
las instrucciones, el conocimiento y la carpeta assets/.
"""
import argparse
import json
import sys
import zipfile
from pathlib import Path

IGNORAR = {'.DS_Store', 'Thumbs.db'}


def archivos(raiz, rutas):
    for ruta in rutas:
        p = raiz / ruta
        if not p.exists():
            sys.exit('No existe %s' % p)
        if p.is_dir():
            for f in sorted(p.rglob('*')):
                if f.is_file() and f.name not in IGNORAR and not f.name.startswith('.'):
                    yield f
        else:
            yield p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('carpeta', type=Path)
    ap.add_argument('-o', '--salida', type=Path)
    a = ap.parse_args()
    raiz = a.carpeta.resolve()
    manifest = json.loads((raiz / 'marca.json').read_text(encoding='utf-8'))
    conocimiento = manifest.get('conocimiento') or []
    if isinstance(conocimiento, str):
        conocimiento = [conocimiento]
    incluir = manifest.get('incluir') or (
        ['marca.json', manifest['plantilla'], 'assets']
        + ([manifest['instrucciones']] if manifest.get('instrucciones') else [])
        + conocimiento
    )
    if 'marca.json' not in incluir:
        incluir = ['marca.json'] + incluir
    salida = (a.salida or raiz / ('%s-paquete.zip' % manifest['codigo'])).resolve()
    vistos = set()
    with zipfile.ZipFile(salida, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in archivos(raiz, incluir):
            nombre = f.relative_to(raiz).as_posix()
            if nombre not in vistos and f != salida:
                vistos.add(nombre)
                zf.write(f, nombre)
    print('✓ %s (%d archivos, %.1f MB)' % (salida, len(vistos), salida.stat().st_size / 1e6))


if __name__ == '__main__':
    main()
