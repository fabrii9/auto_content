#!/usr/bin/env python3
"""Prueba los servicios sin Odoo: paquete, render, video y validación del plan.

Uso: python3 tools/probar_servicios.py <paquete.zip> <carpeta-salida> [video.mp4]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import brand_pack, planner, renderer  # noqa: E402

paquete, salida = Path(sys.argv[1]), Path(sys.argv[2])
video = Path(sys.argv[3]) if len(sys.argv) > 3 else None
salida.mkdir(parents=True, exist_ok=True)

data = paquete.read_bytes()
manifest, textos, checksum = brand_pack.read_pack(data)
print('✓ paquete', manifest['codigo'], len(manifest['disenos']), 'diseños,', len(manifest['lugares']), 'lugares')
raiz = brand_pack.extract_pack(data, checksum, 'prueba')
assert brand_pack.extract_pack(data, checksum, 'prueba') == raiz
plantilla = raiz / manifest['plantilla']
chrome = renderer.find_chrome()

sistema = planner.build_system_prompt(manifest['nombre'], manifest['disenos'], manifest['lugares'],
                                      textos['instrucciones'], textos['conocimiento'], 'Ajuste de prueba')
assert 'Lomas Este' in sistema and 'Ajuste de prueba' in sistema and '"diseno"' in sistema
print('✓ prompt de sistema', len(sistema), 'caracteres')

media = [{'index': 1, 'kind': 'image'}, {'index': 2, 'kind': 'video'}]
respuesta = '```json\n' + json.dumps({
    'diseno': 'foto', 'media': '7',
    'datos': {'etiqueta': 'La energía no llega sola', 'titular': 'Trabajamos para *mejorar tu red.*',
              'bajada': 'Cambiamos postes en Lomas Este para que tengas un servicio más seguro.',
              'lugar': 'Lomas Este\nBella Vista', 'foto': 'x'},
    'lugar': 'Lomas Este', 'resumen': 'Foto de la cuadrilla.', 'alertas': 'Sin casco', 'sticker': None,
}) + '\n```'
plan = planner.parse_plan(respuesta, manifest['disenos'], media)
assert plan['media'] == 1 and 'foto' not in plan['datos'] and plan['alertas'] == ['Sin casco'], plan
for mala in ('nada', '{"diseno": "inventado", "datos": {}}', '{"diseno": "foto", "media": 1}'):
    try:
        planner.parse_plan(mala, manifest['disenos'], media)
        raise SystemExit('debió fallar: %s' % mala)
    except planner.PlanError:
        pass
print('✓ validación del plan')

foto = raiz / manifest['foto_prueba']
mensajes = planner.build_messages('openai', 'sys', 'user', [planner.to_jpeg(foto.read_bytes())])
assert mensajes[1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
assert planner.build_messages('anthropic', 's', 'u', [b'x'])[1]['content'][0]['type'] == 'image'
print('✓ mensajes multimodales')

datos = dict(plan['datos'], plantilla='foto', foto=foto.resolve().as_uri())
renderer.render_html(plantilla, datos, salida / 'foto.png', 1080, 1920, chrome)
renderer.render_html(plantilla, dict(plan['datos'], plantilla='foto', foto='', transparente=True),
                     salida / 'capa.png', 1080, 1920, chrome, transparent=True)
aviso = {'plantilla': 'aviso', 'etiqueta': 'Corte programado', 'titular': 'Trabajamos para *mejorar tu red.*',
         'filas': [{'icono': 'calendario', 'k': 'Cuándo', 'v': 'Miércoles 5'}, {'icono': 'zona', 'k': 'Zonas', 'v': 'Lomas Este'},
                   {'icono': 'reloj', 'k': 'Horario', 'v': 'De 8 a 12 h </script> <b>'}]}
renderer.render_html(plantilla, aviso, salida / 'aviso.png', 1080, 1920, chrome)
print('✓ render: foto, capa transparente y aviso en', salida)

if video:
    ffmpeg = renderer.find_ffmpeg()
    renderer.video_frame(video, salida / 'cuadro.jpg', ffmpeg)
    renderer.compose_video(video, salida / 'capa.png', salida / 'video.mp4', 1080, 1920, ffmpeg, max_seconds=5)
    print('✓ video compuesto')
