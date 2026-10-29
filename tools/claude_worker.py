#!/usr/bin/env python3
"""Worker de Contenido automatizado para marcas en modo "Claude Code".

Corre en una PC con Claude Code instalado y logueado con la cuenta del
operador (``claude`` en el PATH). Cada ``AC_INTERVAL`` segundos le pregunta a
Odoo si hay solicitudes esperando IA. Solo cuando hay trabajo ejecuta
``claude -p`` con permiso únicamente para leer las imágenes, y le devuelve la
respuesta a Odoo, que la valida, arma la pieza y la manda por WhatsApp.

Configuración (variables de entorno o un archivo ``claude_worker.env`` junto
al script, con líneas CLAVE=valor):

    AC_ODOO_URL=https://odoo.aftermoves.com
    AC_ODOO_DB=Aftermoves_Produccion
    AC_ODOO_USER=usuario@aftermoves.com
    AC_ODOO_KEY=<API key de Odoo del usuario>
    AC_INTERVAL=30          # segundos entre consultas
    AC_CLAUDE=claude        # ruta al ejecutable si no está en el PATH
    AC_TIMEOUT=600          # segundos máximos por ejecución de Claude
    AC_LOG=                 # archivo de log (vacío: consola)

Uso:
    python3 claude_worker.py            # queda corriendo
    python3 claude_worker.py --once     # procesa lo pendiente y sale
"""
import argparse
import base64
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import xmlrpc.client
from pathlib import Path

log = logging.getLogger('claude_worker')
MODEL = 'auto.content.request'
WORKER_NAME = socket.gethostname()


def load_config():
    env_file = Path(__file__).with_name('claude_worker.env')
    if env_file.exists():
        # utf-8-sig: PowerShell 5 (y el instalador de Windows) escribe el .env con BOM.
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())
    missing = [k for k in ('AC_ODOO_URL', 'AC_ODOO_DB', 'AC_ODOO_USER', 'AC_ODOO_KEY') if not os.environ.get(k)]
    if missing:
        sys.exit('Faltan variables: %s (ver el encabezado del script).' % ', '.join(missing))
    claude = shutil.which(os.environ.get('AC_CLAUDE', 'claude'))
    if not claude:
        sys.exit('No encontré Claude Code. Instalalo y logueate con "claude", o configurá AC_CLAUDE.')
    return {
        'url': os.environ['AC_ODOO_URL'].rstrip('/'),
        'db': os.environ['AC_ODOO_DB'],
        'user': os.environ['AC_ODOO_USER'],
        'key': os.environ['AC_ODOO_KEY'],
        'interval': int(os.environ.get('AC_INTERVAL', '30')),
        'timeout': int(os.environ.get('AC_TIMEOUT', '600')),
        'claude': claude,
    }


class Odoo:

    def __init__(self, cfg):
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        self.cfg = cfg
        common = xmlrpc.client.ServerProxy(cfg['url'] + '/xmlrpc/2/common', context=context)
        self.uid = common.authenticate(cfg['db'], cfg['user'], cfg['key'], {})
        if not self.uid:
            sys.exit('Odoo rechazó el usuario o la API key.')
        self.models = xmlrpc.client.ServerProxy(cfg['url'] + '/xmlrpc/2/object', context=context, allow_none=True)

    def call(self, method, *args):
        return self.models.execute_kw(self.cfg['db'], self.uid, self.cfg['key'], MODEL, method, list(args))


def build_prompt(job, image_names):
    images = '\n'.join('- %s (archivo %d)' % (name, i) for i, name in enumerate(image_names, 1))
    return (
        job['system']
        + '\n\n# Material de esta solicitud\n'
        + job['user']
        + '\n\n# Imágenes\nEstán en el directorio actual. Leé cada una con la herramienta Read antes de decidir:\n'
        + (images or '(no hay imágenes)')
        + '\n\nRespondé solo con el objeto JSON pedido, sin texto antes ni después.'
    )


def run_claude(cfg, model, prompt, workdir):
    command = [
        cfg['claude'], '-p', '--output-format', 'json', '--model', model,
        '--tools', 'Read', '--allowedTools', 'Read',
        '--max-turns', '15', '--no-session-persistence',
    ]
    result = subprocess.run(command, input=prompt, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', cwd=workdir, timeout=cfg['timeout'])
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('Claude Code no respondió JSON (código %s): %s' % (
            result.returncode, (result.stderr or result.stdout)[-500:]))
    if data.get('is_error'):
        raise RuntimeError('Claude Code: %s' % (data.get('result') or data.get('subtype') or 'error'))
    return data.get('result') or ''


def process(odoo, cfg, job):
    log.info('%s (%s): %d imagen(es)', job['name'], job['brand'], len(job['images']))
    workdir = tempfile.mkdtemp(prefix='ac-%s-' % job['id'])
    try:
        names = []
        for i, image in enumerate(job['images'], 1):
            name = 'media-%d.jpg' % i
            Path(workdir, name).write_bytes(base64.b64decode(image))
            names.append(name)
        prompt = build_prompt(job, names)
        for attempt in (1, 2):
            answer = run_claude(cfg, job['model'], prompt, workdir)
            result = odoo.call('worker_submit', job['id'], answer, '%s@%s' % (job['model'], WORKER_NAME))
            if result.get('ok'):
                log.info('%s: lista (%s)', job['name'], result.get('state'))
                return
            if not result.get('retry') or attempt == 2:
                raise RuntimeError(result.get('error') or 'Odoo no aceptó el plan.')
            log.warning('%s: plan inválido (%s), le pido que lo corrija', job['name'], result['error'])
            prompt += '\n\nTu respuesta anterior no sirve: %s\nVolvé a responder solo con el JSON pedido.' % result['error']
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def tick(odoo, cfg):
    jobs = odoo.call('worker_claim_jobs', 1)
    for job in jobs:
        try:
            process(odoo, cfg, job)
        except Exception as exc:
            log.exception('%s: falló', job['name'])
            odoo.call('worker_fail', job['id'], str(exc)[:1000])
    return len(jobs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--once', action='store_true', help='procesa lo pendiente y sale')
    args = ap.parse_args()
    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        filename=os.environ.get('AC_LOG') or None, encoding='utf-8')
    odoo = Odoo(cfg)
    log.info('Conectado a %s como %s. Claude Code: %s', cfg['url'], cfg['user'], cfg['claude'])
    while True:
        try:
            while tick(odoo, cfg):
                pass
        except Exception:
            log.exception('Error consultando Odoo; reintento en %s s', cfg['interval'])
        if args.once:
            return
        time.sleep(cfg['interval'])


if __name__ == '__main__':
    main()
