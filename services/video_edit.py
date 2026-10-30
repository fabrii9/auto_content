# -*- coding: utf-8 -*-
"""Montaje de videos: análisis del material para la IA y render del guion.

La IA no edita: mira hojas de contacto (cuadros con su segundo escrito), los
cambios de escena y, si hay, lo que se dice, y devuelve un guion (clips,
transiciones, rótulos, música). Este módulo lo ejecuta con ffmpeg en dos
pasos: cada clip se normaliza a un segmento igual a los demás (tamaño, cuadros
por segundo, audio) y después se unen con las transiciones, las capas de
marca, los subtítulos y la música.
"""

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .renderer import _CHILD, RenderError

# Nombre en el guion → transición de xfade (None: corte directo).
TRANSITIONS = {
    'corte': None,
    'fundido': 'fade',
    'disolver': 'dissolve',
    'negro': 'fadeblack',
    'blanco': 'fadewhite',
    'barrido': 'slideleft',
    'deslizar': 'slideup',
    'zoom': 'zoomin',
    'circulo': 'circleopen',
}
FPS = 30
MAX_FRAMES_PER_VIDEO = 24
FRAMES_PER_SHEET = 12
SHEET_COLUMNS = 4
TILE = 300
SEGMENT_CODEC = [
    # Intermedios: rápidos y casi sin pérdida; la calidad final la fija la última pasada.
    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '16', '-g', str(FPS), '-pix_fmt', 'yuv420p',
    '-video_track_timescale', '15360', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
]
SILENCE = ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']


def find_ffprobe(ffmpeg):
    sibling = Path(ffmpeg).with_name('ffprobe')
    path = str(sibling) if sibling.exists() else shutil.which('ffprobe')
    if not path:
        raise RenderError('No encontré ffprobe (viene con ffmpeg).')
    return path


def _run(command, timeout, cwd=None):
    """Corre ffmpeg con prioridad baja: el servidor es compartido."""
    if shutil.which('nice'):
        command = ['nice', '-n', '10'] + command
    try:
        result = subprocess.run([str(c) for c in command], capture_output=True, timeout=timeout,
                                cwd=cwd, **_CHILD)
    except subprocess.TimeoutExpired as exc:
        raise RenderError('ffmpeg tardó más de %s s.' % timeout) from exc
    if result.returncode != 0:
        raise RenderError('ffmpeg falló (código %s): %s' % (
            result.returncode, result.stderr.decode('utf-8', 'replace')[-800:] or 'sin mensaje'))
    return result


# ----------------------------------------------------------------------
# Análisis (lo que ve la IA)
# ----------------------------------------------------------------------
def probe(path, ffprobe):
    """Duración, tamaño ya rotado y si tiene audio."""
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)],
        capture_output=True, timeout=60, **_CHILD,
    )
    if result.returncode != 0:
        raise RenderError('No pude leer el video: %s' % result.stderr.decode('utf-8', 'replace')[-300:])
    info = json.loads(result.stdout or b'{}')
    streams = info.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not video:
        raise RenderError('El archivo no tiene video.')
    width, height = int(video.get('width') or 0), int(video.get('height') or 0)
    rotation = 0
    for side in video.get('side_data_list') or []:
        if 'rotation' in side:
            rotation = int(float(side['rotation']))
    rotation = rotation or int(float((video.get('tags') or {}).get('rotate') or 0))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    duration = float((info.get('format') or {}).get('duration') or video.get('duration') or 0)
    if duration <= 0:
        raise RenderError('No pude saber cuánto dura el video.')
    return {
        'duracion': round(duration, 2),
        'ancho': width,
        'alto': height,
        'audio': any(s.get('codec_type') == 'audio' for s in streams),
    }


def scene_changes(path, ffmpeg, threshold=0.3, limit=40):
    """Segundos donde cambia la toma (candidatos a corte)."""
    command = [ffmpeg, '-hide_banner', '-nostats', '-i', path, '-an',
               '-vf', "scale=240:-2,select='gt(scene,%s)',showinfo" % threshold, '-f', 'null', '-']
    try:
        result = _run(command, timeout=300)
    except RenderError:
        return []
    times = []
    for match in re.findall(rb'pts_time:([\d.]+)', result.stderr):
        value = round(float(match), 1)
        if not times or value - times[-1] >= 1.0:
            times.append(value)
    return times[:limit]


def _label_font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def contact_sheets(path, duration, workdir, prefix, ffmpeg, max_frames=MAX_FRAMES_PER_VIDEO):
    """Arma hojas de contacto (JPEG) con cuadros repartidos en todo el video y
    el segundo de cada uno escrito arriba a la izquierda.

    Devuelve una lista de dicts ``{'path', 'desde', 'hasta', 'paso'}``.
    """
    from PIL import Image, ImageDraw

    workdir = Path(workdir)
    count = max(1, min(max_frames, int(duration)))
    step = duration / count
    frames = []
    for i in range(count):
        at = round(step * (i + 0.5), 1)
        out = workdir / ('%s-cuadro-%02d.jpg' % (prefix, i))
        try:
            _run([ffmpeg, '-y', '-loglevel', 'error', '-ss', at, '-i', path, '-frames:v', '1',
                  '-vf', 'scale=%d:%d:force_original_aspect_ratio=decrease' % (TILE, TILE), '-q:v', '4', out],
                 timeout=60)
        except RenderError:
            continue
        if out.exists() and out.stat().st_size:
            frames.append((at, out))
    if not frames:
        raise RenderError('No pude sacar cuadros del video.')

    font = _label_font(30)
    sheets = []
    for number, start in enumerate(range(0, len(frames), FRAMES_PER_SHEET), 1):
        chunk = frames[start:start + FRAMES_PER_SHEET]
        rows = math.ceil(len(chunk) / SHEET_COLUMNS)
        sheet = Image.new('RGB', (SHEET_COLUMNS * TILE, rows * TILE), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for i, (at, frame_path) in enumerate(chunk):
            x, y = (i % SHEET_COLUMNS) * TILE, (i // SHEET_COLUMNS) * TILE
            with Image.open(frame_path) as frame:
                frame = frame.convert('RGB')
                sheet.paste(frame, (x + (TILE - frame.width) // 2, y + (TILE - frame.height) // 2))
            label = '%.1f s' % at
            box = draw.textbbox((0, 0), label, font=font)
            draw.rectangle((x + 4, y + 4, x + box[2] + 16, y + box[3] + 12), fill=(255, 214, 0))
            draw.text((x + 10, y + 6), label, fill=(0, 0, 0), font=font)
        out = workdir / ('%s-hoja-%d.jpg' % (prefix, number))
        sheet.save(out, format='JPEG', quality=82)
        sheets.append({'path': out, 'desde': chunk[0][0], 'hasta': chunk[-1][0], 'paso': round(step, 1)})
    return sheets


def extract_audio(path, out, ffmpeg):
    """Audio mono 16 kHz en Opus, chico, para transcribir. ``None`` si no se pudo."""
    try:
        _run([ffmpeg, '-y', '-loglevel', 'error', '-i', path, '-vn', '-ac', '1', '-ar', '16000',
              '-c:a', 'libopus', '-b:a', '24k', out], timeout=180)
    except RenderError:
        return None
    return Path(out) if Path(out).exists() and Path(out).stat().st_size else None


# ----------------------------------------------------------------------
# Línea de tiempo
# ----------------------------------------------------------------------
def frames(seconds):
    """Redondea a cuadros enteros: los cortes caen justo en un cuadro."""
    return round(round(seconds * FPS) / FPS, 3)


def transition_length(kind, previous, current, default):
    if not TRANSITIONS.get(kind):
        return 0.0
    length = frames(min(default, previous / 2, current / 2))
    return length if length >= 0.1 else 0.0


def timeline(clips, default_transition=0.4):
    """Inicio de cada clip en el video final y duración total.

    Las transiciones (salvo el corte) superponen el final de un clip con el
    principio del siguiente, así que acortan el total.
    """
    starts, total = [], 0.0
    for i, clip in enumerate(clips):
        if i == 0:
            starts.append(0.0)
            total = clip['duracion']
            continue
        overlap = transition_length(clip['transicion'], clips[i - 1]['duracion'], clip['duracion'],
                                    default_transition)
        starts.append(round(total - overlap, 3))
        total = total - overlap + clip['duracion']
    return starts, round(total, 3)


def format_time(seconds):
    seconds = max(0.0, seconds)
    return '%d:%04.1f' % (seconds // 60, seconds % 60)


# ----------------------------------------------------------------------
# Subtítulos (ASS)
# ----------------------------------------------------------------------
def _words(transcript):
    """Palabras con tiempos; si el segmento no las trae, se reparten parejo."""
    words = []
    for segment in (transcript or {}).get('segmentos') or []:
        items = segment.get('palabras') or []
        if items:
            words += [w for w in items if (w.get('texto') or '').strip()]
            continue
        tokens = (segment.get('texto') or '').split()
        if not tokens:
            continue
        span = (segment['fin'] - segment['inicio']) / len(tokens)
        words += [{'inicio': segment['inicio'] + span * i, 'fin': segment['inicio'] + span * (i + 1), 'texto': t}
                  for i, t in enumerate(tokens)]
    return words


def _chunks(words, max_words=5, max_chars=30, max_gap=0.6):
    chunk = []
    for word in words:
        text = word['texto'].strip()
        if chunk and (
            len(chunk) >= max_words
            or len(' '.join(w['texto'].strip() for w in chunk) + ' ' + text) > max_chars
            or word['inicio'] - chunk[-1]['fin'] > max_gap
        ):
            yield chunk
            chunk = []
        chunk.append(word)
        if text[-1:] in '.?!':
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def subtitle_events(clips, starts, transcripts):
    """``[(inicio, fin, texto)]`` en el tiempo del video final."""
    events = []
    for clip, start in zip(clips, starts):
        if clip['tipo'] != 'video':
            continue
        speed = clip['velocidad']
        end = start + clip['duracion']
        words = [w for w in _words(transcripts.get(str(clip['archivo'])))
                 if w['inicio'] >= clip['desde'] - 0.05 and w['fin'] <= clip['hasta'] + 0.25]
        for chunk in _chunks(words):
            begin = max(start, start + (chunk[0]['inicio'] - clip['desde']) / speed)
            finish = min(end, start + (chunk[-1]['fin'] - clip['desde']) / speed)
            finish = min(end, max(finish, begin + 0.7))
            if finish > begin:
                events.append([begin, finish, ' '.join(w['texto'].strip() for w in chunk)])
    events.sort()
    for current, following in zip(events, events[1:]):
        current[1] = min(current[1], following[0])
    return [tuple(e) for e in events if e[1] - e[0] >= 0.2]


def _ass_color(hex_color, alpha=0):
    value = (hex_color or '#FFFFFF').lstrip('#')
    if len(value) != 6:
        value = 'FFFFFF'
    r, g, b = value[0:2], value[2:4], value[4:6]
    return '&H%02X%s%s%s' % (alpha, b, g, r)


def _ass_time(seconds):
    centis = int(round(seconds * 100))
    return '%d:%02d:%02d.%02d' % (centis // 360000, centis // 6000 % 60, centis // 100 % 60, centis % 100)


def build_ass(events, style, width, height):
    style = style or {}
    lines = [
        '[Script Info]', 'ScriptType: v4.00+', 'PlayResX: %d' % width, 'PlayResY: %d' % height,
        'WrapStyle: 0', 'ScaledBorderAndShadow: yes', '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
        'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding',
        'Style: Sub,%s,%d,%s,%s,%s,%s,-1,0,0,0,100,100,0,0,3,14,0,2,90,90,%d,1' % (
            style.get('familia') or 'Sans', int(style.get('tamano') or 60),
            _ass_color(style.get('color')), _ass_color(style.get('color')),
            _ass_color(style.get('borde') or '#000000'), _ass_color(style.get('borde') or '#000000', 0x30),
            int(style.get('margen_inferior') or 480),
        ),
        '',
        '[Events]',
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
    ]
    for begin, end, text in events:
        text = text.replace('\\', '').replace('{', '(').replace('}', ')').replace('\n', ' ')
        lines.append('Dialogue: 0,%s,%s,Sub,,0,0,0,,%s' % (_ass_time(begin), _ass_time(end), text))
    return '\n'.join(lines) + '\n'


# ----------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------
def _fit(width, height, focus=0.5):
    """Cubre el cuadro y recorta; ``focus`` corre el recorte horizontal (0 izquierda, 1 derecha)."""
    return ('scale=%(w)d:%(h)d:force_original_aspect_ratio=increase,'
            'crop=%(w)d:%(h)d:(iw-%(w)d)*%(f).3f:(ih-%(h)d)/2') % {'w': width, 'h': height, 'f': focus}


def _video_segment(clip, source, out, width, height, ffmpeg, workdir, keep_audio):
    start, length, speed = clip['desde'], clip['hasta'] - clip['desde'], clip['velocidad']
    filters = []
    if clip.get('estabilizar'):
        transforms = '%s.trf' % Path(out).stem
        _run([ffmpeg, '-y', '-loglevel', 'error', '-ss', start, '-t', length, '-i', source['path'],
              '-vf', 'vidstabdetect=shakiness=6:accuracy=12:result=%s' % transforms, '-f', 'null', '-'],
             timeout=300, cwd=workdir)
        filters.append('vidstabtransform=input=%s:smoothing=20:zoom=3,unsharp=5:5:0.6:3:3:0.3' % transforms)
    if speed != 1:
        filters.append('setpts=PTS/%s' % speed)
    filters += [_fit(width, height, clip.get('encuadre', 0.5)), 'fps=%d' % FPS, 'setsar=1', 'format=yuv420p']
    command = [ffmpeg, '-y', '-loglevel', 'error', '-ss', start, '-t', length, '-i', source['path']]
    if keep_audio and source.get('audio'):
        audio = 'aresample=48000,aformat=channel_layouts=stereo'
        if speed != 1:
            audio += ',atempo=%s' % speed
        command += ['-vf', ','.join(filters), '-af', audio, '-map', '0:v:0', '-map', '0:a:0']
    else:
        command += SILENCE + ['-vf', ','.join(filters), '-map', '0:v:0', '-map', '1:a:0']
    command += ['-t', clip['duracion']] + SEGMENT_CODEC + [out]
    _run(command, timeout=300, cwd=workdir)


def _still_segment(image, duration, out, width, height, ffmpeg, workdir, zoom=True):
    """Foto (con un zoom lento) o placa fija como segmento de video."""
    frames = max(1, int(round(duration * FPS)))
    rate = 0.10 / frames if zoom else 0
    filters = (
        'scale=%(W)d:%(H)d:force_original_aspect_ratio=increase,crop=%(W)d:%(H)d,'
        "zoompan=z='min(zoom+%(r).6f,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        ':d=%(n)d:s=%(w)dx%(h)d:fps=%(fps)d,setsar=1,format=yuv420p'
    ) % {'W': width * 2, 'H': height * 2, 'w': width, 'h': height, 'r': rate, 'n': frames, 'fps': FPS}
    command = [ffmpeg, '-y', '-loglevel', 'error', '-i', image] + SILENCE + [
        '-vf', filters, '-map', '0:v:0', '-map', '1:a:0', '-t', duration] + SEGMENT_CODEC + [out]
    _run(command, timeout=300, cwd=workdir)


def _join_video(clips, segments, overlaps, ffmpeg, workdir):
    """Une la imagen de los segmentos por tramos: el cuerpo de cada clip y, entre
    dos clips con transición, un tramo corto con el xfade de sus puntas. Cada
    tramo se codifica solo (poca memoria). Devuelve la lista de tramos: la pasada final los lee en orden con el demuxer concat."""
    pieces = []

    def encode(command, name):
        out = Path(workdir) / name
        _run([ffmpeg, '-y', '-loglevel', 'error'] + command + [
            '-an', '-r', str(FPS)] + SEGMENT_CODEC[:12] + [out.name], timeout=300, cwd=workdir)
        pieces.append(out)

    for i, (clip, segment) in enumerate(zip(clips, segments)):
        if overlaps[i]:
            previous = clips[i - 1]['duracion']
            encode([
                '-ss', '%.3f' % (previous - overlaps[i]), '-t', '%.3f' % overlaps[i], '-i', segments[i - 1].name,
                '-t', '%.3f' % overlaps[i], '-i', segment.name,
                '-filter_complex', '[0:v]setpts=PTS-STARTPTS,settb=1/%(fps)d[a];[1:v]setpts=PTS-STARTPTS,settb=1/%(fps)d[b];'
                '[a][b]xfade=transition=%(kind)s:duration=%(d).3f:offset=0[v]' % {
                    'fps': FPS, 'kind': TRANSITIONS[clip['transicion']], 'd': overlaps[i]},
                '-map', '[v]',
            ], 'tramo-%02d-transicion.mp4' % i)
        following = overlaps[i + 1] if i + 1 < len(clips) else 0.0
        body = clip['duracion'] - overlaps[i] - following
        if body >= 1.0 / FPS:
            encode(['-ss', '%.3f' % overlaps[i], '-t', '%.3f' % body, '-i', segment.name, '-map', '0:v:0'],
                   'tramo-%02d-cuerpo.mp4' % i)
    listing = Path(workdir) / 'tramos.txt'
    listing.write_text(''.join("file '%s'\n" % piece.name for piece in pieces))
    return listing


def render_montage(clips, sources, out_path, width, height, ffmpeg, workdir, *,
                   audio_mode='original', music=None, capa=None, capa_hasta=None,
                   rotulos=None, transcripts=None, subtitle_style=None, font_path=None,
                   default_transition=0.4):
    """Arma el video final.

    ``clips`` viene validado por ``planner.parse_montage`` (el cierre, si hay,
    ya va como último clip de tipo ``placa``). ``sources`` mapea el número de
    archivo a ``{'path', 'audio'}``. ``capa`` es el PNG transparente de la
    marca (visible hasta ``capa_hasta``), ``rotulos`` mapea el índice de clip a
    su PNG transparente y ``music`` es ``{'path', 'volumen'}``.

    Devuelve ``(ruta, inicios, total, subtitulos)``.
    """
    workdir = Path(workdir)
    keep_audio = audio_mode != 'musica'
    segments = []
    for i, clip in enumerate(clips):
        out = workdir / ('seg-%02d.mp4' % i)
        if clip['tipo'] == 'video':
            _video_segment(clip, sources[clip['archivo']], out, width, height, ffmpeg, workdir, keep_audio)
        elif clip['tipo'] == 'foto':
            _still_segment(sources[clip['archivo']]['path'], clip['duracion'], out, width, height, ffmpeg, workdir)
        else:
            _still_segment(clip['imagen'], clip['duracion'], out, width, height, ffmpeg, workdir, zoom=False)
        segments.append(out)

    starts, total = timeline(clips, default_transition)
    overlaps = [0.0] + [
        transition_length(clips[i]['transicion'], clips[i - 1]['duracion'], clips[i]['duracion'], default_transition)
        for i in range(1, len(clips))
    ]
    joined = _join_video(clips, segments, overlaps, ffmpeg, workdir)

    # Pasada final sobre un solo video: el audio se arma con los segmentos (es
    # liviano) y la imagen ya viene unida, así ffmpeg no decodifica todo junto.
    inputs, graph = ['-f', 'concat', '-safe', '0', '-i', joined.name], []
    count = 1 + len(segments)
    for i, segment in enumerate(segments):
        inputs += ['-i', segment.name]
        graph.append('[%d:a]asetpts=PTS-STARTPTS[a%d]' % (i + 1, i))
    graph.append('[0:v]setpts=PTS-STARTPTS,format=yuv420p[base]')
    video, audio = 'base', 'a0'
    for i in range(1, len(clips)):
        if overlaps[i]:
            graph.append('[%s][a%d]acrossfade=d=%.3f[ax%d]' % (audio, i, overlaps[i], i))
        else:
            graph.append('[%s][a%d]concat=n=2:v=0:a=1[ax%d]' % (audio, i, i))
        audio = 'ax%d' % i

    def overlay_png(png, begin, end, label):
        nonlocal video, count
        index, count = count, count + 1
        inputs.extend(['-loop', '1', '-framerate', str(FPS), '-t', '%.3f' % total, '-i', str(png)])
        fades = 'fade=t=in:st=%.3f:d=0.3:alpha=1,fade=t=out:st=%.3f:d=0.4:alpha=1' % (
            begin, max(begin, end - 0.4))
        graph.append('[%d:v]format=rgba,%s[%s]' % (index, fades, label))
        graph.append("[%s][%s]overlay=0:0:enable='between(t,%.3f,%.3f)'[%so]" % (video, label, begin, end, label))
        video = '%so' % label

    if capa:
        overlay_png(capa, 0.0, min(total, capa_hasta or total), 'capa')
    capa_end = min(total, capa_hasta or total) if capa else 0.0
    for i, png in sorted((rotulos or {}).items()):
        # El rótulo no se pisa con el titular de la capa: entra cuando la capa se fue.
        begin = max(starts[i] + 0.4, capa_end + 0.1 if capa else 0.0)
        end = starts[i] + clips[i]['duracion'] - 0.4
        if end - begin >= 0.8:
            overlay_png(png, begin, end, 'rot%d' % i)

    events = subtitle_events(clips, starts, transcripts or {}) if transcripts else []
    if capa:
        # Mientras se ve la capa de marca, su titular ocupa el lugar de los subtítulos.
        hidden_until = min(total, capa_hasta or total)
        events = [e for e in events if e[0] >= hidden_until]
    if events:
        fonts = workdir / 'fuentes'
        fonts.mkdir(exist_ok=True)
        if font_path and Path(font_path).exists():
            shutil.copy(font_path, fonts / Path(font_path).name)
        (workdir / 'subtitulos.ass').write_text(build_ass(events, subtitle_style, width, height), encoding='utf-8')
        graph.append('[%s]subtitles=subtitulos.ass:fontsdir=fuentes[vsub]' % video)
        video = 'vsub'

    loudness = 'loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000'
    if music and audio_mode in ('musica', 'mezcla'):
        index = count
        inputs += ['-stream_loop', '-1', '-i', str(music['path'])]
        level = float(music.get('volumen') or 0.25)
        if audio_mode == 'musica':
            level = min(1.0, level * 3)
        graph.append(
            '[%d:a]atrim=0:%.3f,asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,'
            'volume=%.3f,afade=t=in:d=0.5,afade=t=out:st=%.3f:d=1.5[mus]' % (index, total, level, max(0, total - 1.5)))
        if audio_mode == 'musica':
            graph.append('[%s]anullsink' % audio)
            graph.append('[mus]%s[aout]' % loudness)
        else:
            graph.append('[%s]asplit=2[voz][guia]' % audio)
            graph.append('[mus][guia]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=500[fondo]')
            graph.append('[voz][fondo]amix=inputs=2:duration=first:normalize=0,%s[aout]' % loudness)
    else:
        graph.append('[%s]%s[aout]' % (audio, loudness))

    command = [ffmpeg, '-y', '-loglevel', 'error'] + inputs + [
        '-filter_complex', ';'.join(graph), '-map', '[%s]' % video, '-map', '[aout]',
        '-t', '%.3f' % total, '-r', str(FPS), '-threads', '3',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-maxrate', '10M', '-bufsize', '20M',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart', Path(out_path).name,
    ]
    _run(command, timeout=480, cwd=workdir)
    out = workdir / Path(out_path).name
    if out != Path(out_path):
        shutil.move(out, out_path)
    return Path(out_path), starts, total, events


def make_preview(video_path, out_path, ffmpeg, width=720, height=1280):
    """Versión liviana (hasta ~1,5 Mb/s) para mandar como video por WhatsApp."""
    _run([ffmpeg, '-y', '-loglevel', 'error', '-i', video_path,
          '-vf', 'scale=%d:%d' % (width, height), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '27',
          '-maxrate', '1500k', '-bufsize', '3000k', '-pix_fmt', 'yuv420p',
          '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', out_path], timeout=300)
    return Path(out_path)
