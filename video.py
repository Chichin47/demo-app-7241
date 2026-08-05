#!/usr/bin/env python3
"""Motor de armado de reels verticales.

Convierte una publicación (fotos o un clip) en un video vertical de 1080x1920
con la misma estructura que se usa en este tipo de contenido:

    ┌──────────────────────────┐
    │  banner: sticker +       │  ← el fondo de toda la pantalla es la misma
    │  título en amarillo      │    imagen ampliada y desenfocada
    ├──────────────────────────┤
    │                          │
    │  franja central: la foto │  ← acá va el movimiento (zoom y paneos
    │  o el clip, con vida     │    suaves) y encima los subtítulos
    │                          │
    ├──────────────────────────┤
    │  franja de abajo: otra   │  ← también desenfocada, igual que la de
    │  toma, desenfocada       │    arriba: la foto nítida es solo el centro
    └──────────────────────────┘

Todo el trabajo pesado lo hace ffmpeg, que ya viene instalado en las máquinas
donde corre el bot, así que no hay nada que pagar ni que instalar.

El armado se hace por partes y no en un solo comando gigante: cada franja se
renderiza a un archivo temporal y al final se pegan todas. Es un pelín más
lento, pero cuando algo sale mal se ve exactamente en qué franja fue, que es lo
que uno agradece a las tres de la mañana.

Uso:

    import video
    video.armar(
        salida="reel.mp4",
        fotos=["a.jpg", "b.jpg"],     # o clip="origen.mp4"
        titulo="¡LA OUIJA DESATÓ EL CAOS!",
        audio="voz.mp3",              # opcional
        subtitulos="subs.ass",        # opcional
    )
"""
import os
import re
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FUENTE_TITULO = BASE_DIR / "fonts" / "Anton-Regular.ttf"
FUENTE_ALTERNA = BASE_DIR / "fonts" / "LuckiestGuy-Regular.ttf"
CARPETA_ASSETS = BASE_DIR / "assets"

# Medidas del lienzo. Las tres franjas suman exactamente el alto total.
LIENZO_W, LIENZO_H = 1080, 1920
# Con sticker arriba el banner necesita aire; sin sticker ese espacio quedaría
# vacío y desenfocado, así que se le regala a la franja del medio.
BANNER_CON_STICKER = 660
BANNER_SIN_STICKER = 430
PIE_H = 520
# Valores por defecto, para el código que los use suelto.
BANNER_H = BANNER_CON_STICKER
CENTRO_H = LIENZO_H - BANNER_H - PIE_H  # 740


def medidas(hay_sticker=True):
    """Alto de cada franja: (banner, centro, pie). Siempre suman 1920."""
    banner = BANNER_CON_STICKER if hay_sticker else BANNER_SIN_STICKER
    return banner, LIENZO_H - banner - PIE_H, PIE_H


def margen_subtitulos(hay_sticker=True):
    """A qué altura del borde de abajo van los subtítulos.

    Se calcula desde el alto de la franja de abajo para que el texto quede
    siempre apoyado dentro de la franja central, sin pisar la de abajo.
    """
    return medidas(hay_sticker)[2] + 100

FPS = 30
# Techo del reel. OJO: esto NO es la duración a la que apunta el video, es el
# freno de mano. La duración de verdad la manda el guion, y el guion la manda la
# historia: reseña, diálogo y remate completos. Si eso entra en 22 segundos, el
# reel dura 22 y está bien; si necesita 45, dura 45 y también está bien.
#
# El número fue subiendo por una sola razón: cada vez que era chico, cortaba
# historias justo antes del golpe (30 cortaba, 34 seguía apretando en los posts
# de seis o siete intervenciones). Cincuenta y dos deja llegar cómodo a los
# cincuenta segundos hablados sin que la última palabra quede pisada, y sigue
# frenando el caso raro en que el guion se desmadre.
DURACION_MAX = 52.0
DURACION_MIN = 6.0

# El acabado de las franjas que NO son la del centro: desenfoque fuerte, un
# punto menos de luz y un punto más de color. Es lo que hace que la vista se
# vaya sola a la foto nítida del medio. Va en un solo sitio para que la de
# arriba y la de abajo salgan siempre iguales.
DESENFOQUE = "gblur=sigma=34,eq=brightness=-0.10:saturation=1.15"

# Cuánto dura cada toma antes de pasar a la siguiente. Tres segundos es el
# punto justo: alcanza para que un paneo cruce la foto despacio y no da tiempo
# a aburrirse. Menos que esto obliga a mover más rápido para recorrer lo mismo,
# y ahí es donde el movimiento se siente brusco.
SEGUNDOS_POR_TOMA = 3.0
# Cuánto dura el cruce de una toma a la otra. Medio segundo: se nota el
# desvanecimiento pero no se hace lento. En cero vuelve al corte seco.
CRUCE = 0.5

# Los cruces se van turnando, uno distinto en cada empalme, para que el video
# no se sienta siempre igual. Todos son suaves a propósito: desvanecidos,
# barridos blandos y aperturas. Nada de cortes duros ni de pasar por negro, que
# en un reel corto se leen como un error de armado.
TRANSICIONES = (
    "fade",          # el clásico: una se apaga mientras la otra aparece
    "smoothleft",    # barrido blando hacia la izquierda
    "dissolve",      # se disuelve por manchas, muy orgánico
    "circleopen",    # se abre un círculo desde el centro
    "smoothup",      # barrido blando hacia arriba
    "hblur",         # se desenfoca, cambia, y vuelve a enfocar
    "smoothright",
    "radial",        # gira como una aguja de reloj
    "smoothdown",
    "circleclose",
)

AMARILLO = (255, 214, 10)
NEGRO = (12, 12, 12)

# Rango aproximado de emojis, para separarlos del título: la fuente del título
# no los sabe dibujar y hay que ponerlos aparte con la fuente de color.
RE_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿←-⇿]+"
)


class ErrorDeVideo(RuntimeError):
    """Algo falló armando el video. Trae el final del log de ffmpeg."""


def log(msg):
    print(f"[video] {msg}", flush=True)


def _correr(args, etiqueta, cwd=None, timeout=600):
    """Corre ffmpeg y, si falla, levanta un error con la parte útil del log."""
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        cola_log = (proc.stderr or "").strip().splitlines()[-12:]
        raise ErrorDeVideo(f"{etiqueta} falló:\n" + "\n".join(cola_log))
    return proc


def duracion(ruta):
    """Cuántos segundos dura un archivo de audio o video."""
    try:
        salida = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(ruta)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(salida)
    except Exception:
        return 0.0


def tiene_audio(ruta):
    """Dice si el archivo trae pista de audio."""
    try:
        salida = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", str(ruta)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return bool(salida)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Movimiento sobre las fotos
# ---------------------------------------------------------------------------

#: Nombres de los movimientos, en el orden en que se van turnando. El orden
#: importa: nunca se repite el mismo dos tomas seguidas y siempre alterna
#: desplazamiento con zoom, que es lo que hace que la foto parezca viva. Son
#: diez, así que en un reel de treinta segundos no se repite ninguno.
MOVIMIENTOS = (
    "acercar",
    "paneo_derecha",
    "alejar",
    "paneo_arriba",
    "acercar_derecha",
    "paneo_izquierda",
    "respiro",
    "paneo_abajo",
    "alejar_izquierda",
    "deriva_diagonal",
)

# Cuánto se agranda la foto en los paneos. Este número es, ni más ni menos, lo
# que el paneo va a recorrer: con 1.22 la imagen se corre un 22% del ancho de
# la pantalla de punta a punta de la toma. Antes era 1.30 recorrido dos veces
# de ida y vuelta dentro de la misma toma, o sea 120% en dos segundos y medio,
# y por eso el movimiento lateral se veía brusco. Ahora es un solo viaje suave.
ZOOM_PANEO = 1.22


def _movimiento(nombre, cuadros):
    """Expresiones de zoom y desplazamiento para una toma.

    Todo se calcula sobre «on», que es el número de cuadro que va saliendo.

    La clave de que se vea suave está en `suave`: en vez de correr la foto a
    velocidad pareja (que arranca de golpe y frena de golpe), se usa media
    curva de coseno. Eso hace que la toma empiece quieta, tome velocidad en el
    medio y se detenga sola al final, que es como se mueve una cámara de
    verdad. Y cada toma hace UN solo viaje: nada de ir y volver, que era lo que
    daba el efecto de sacudida.
    """
    n = max(1, cuadros)
    # Recorridos disponibles dentro de la imagen ampliada.
    ancho_libre = "(iw-iw/zoom)"
    alto_libre = "(ih-ih/zoom)"
    # Va de 0 a 1 una sola vez, entrando y saliendo despacio.
    suave = f"(1-cos(PI*on/{n}))/2"
    # Va de 0 a 1 y vuelve a 0. Solo para el latido y para la franja de abajo.
    ida_y_vuelta = f"(1-cos(2*PI*on/{n}))/2"
    centro_x, centro_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    z = f"{ZOOM_PANEO:.2f}"

    # --- Paneos: la foto se corre, el encuadre no cambia de tamaño.
    if nombre == "paneo_derecha":
        return z, f"{ancho_libre}*{suave}", centro_y
    if nombre == "paneo_izquierda":
        return z, f"{ancho_libre}*(1-{suave})", centro_y
    if nombre == "paneo_abajo":
        return z, centro_x, f"{alto_libre}*{suave}"
    if nombre == "paneo_arriba":
        return z, centro_x, f"{alto_libre}*(1-{suave})"
    if nombre == "deriva_diagonal":
        # Cruza en diagonal, pero recorriendo menos en cada eje: si no, dos
        # movimientos juntos vuelven a sentirse rápidos.
        return "1.24", f"{ancho_libre}*(0.15+0.70*{suave})", \
               f"{alto_libre}*(0.85-0.70*{suave})"

    # --- Zooms puros.
    if nombre == "acercar":
        return f"1.04+0.22*{suave}", centro_x, centro_y
    if nombre == "alejar":
        return f"1.26-0.22*{suave}", centro_x, centro_y
    if nombre == "respiro":
        # Un latido apenas perceptible, para las tomas de descanso.
        return f"1.08+0.09*{ida_y_vuelta}", centro_x, centro_y

    # --- Mezclas: se acerca mientras se corre, como un travelling.
    if nombre == "acercar_derecha":
        return f"1.06+0.20*{suave}", f"{ancho_libre}*(0.30+0.40*{suave})", centro_y
    if nombre == "alejar_izquierda":
        return f"1.26-0.20*{suave}", f"{ancho_libre}*(0.70-0.40*{suave})", centro_y

    if nombre == "pie_cerrado":
        # Para la franja de abajo: encuadre cerrado y un solo viaje de ida y
        # vuelta repartido en todo el reel, o sea lentísimo. Como además va
        # desenfocada, alcanza para que la parte de abajo respire.
        return "1.32", f"{ancho_libre}*{ida_y_vuelta}", centro_y
    return "1.15", centro_x, centro_y


def _clip_de_foto(foto, segundos, ancho, alto, salida, movimiento="acercar",
                  acabado=""):
    """Convierte una foto fija en una toma con movimiento.

    La foto se agranda al doble del tamaño final antes de moverla: así el zoom
    no la pixela y el vaivén tiene de dónde sacar imagen para correrse.

    `acabado` son filtros extra que se aplican YA con el tamaño final (por
    ejemplo el desenfoque de la franja de abajo). Van después del movimiento a
    propósito: desenfocar al final cuesta mucho menos que desenfocar el doble
    de píxeles antes de recortar, y se ve exactamente igual.
    """
    cuadros = max(2, int(round(segundos * FPS)))
    z, x, y = _movimiento(movimiento, cuadros)
    grande_w, grande_h = ancho * 2, alto * 2
    filtro = (
        f"scale={grande_w}:{grande_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={grande_w}:{grande_h},setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={ancho}x{alto}:fps={FPS},"
        + (f"{acabado}," if acabado else "")
        + "format=yuv420p"
    )
    _correr(
        ["ffmpeg", "-y", "-v", "error",
         "-loop", "1", "-framerate", str(FPS), "-t", f"{segundos:.3f}", "-i", str(foto),
         "-vf", filtro, "-frames:v", str(cuadros),
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         str(salida)],
        f"clip de la foto {Path(foto).name}",
    )
    return salida


def _encadenar(clips, salida, tmp, arranque=0):
    """Pega las tomas una detrás de otra, cruzando de una a la siguiente.

    Cada empalme usa un cruce distinto, sacado de TRANSICIONES y turnándose:
    una se desvanece, la otra se disuelve, la de más allá abre un círculo. Es
    lo que hace que el video no se sienta siempre igual aunque la foto sea
    siempre la misma. Con CRUCE en cero vuelve al corte seco de antes, que se
    pega sin recodificar y es instantáneo.

    `arranque` corre el turno de las transiciones, para que dos reels seguidos
    no empiecen con el mismo cruce.
    """
    if len(clips) == 1:
        shutil.copy(clips[0], salida)
        return salida

    if CRUCE <= 0:
        # Corte seco: se listan las tomas y se pegan sin recodificar. Es
        # instantáneo y no pierde ni un gramo de calidad.
        lista = tmp / "tomas.txt"
        lista.write_text(
            "".join(f"file '{Path(c).resolve()}'\n" for c in clips), encoding="utf-8"
        )
        _correr(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(lista), "-c", "copy", str(salida)],
            "unión de las tomas",
        )
        return salida

    entradas = []
    for c in clips:
        entradas += ["-i", str(c)]
    duraciones = [duracion(c) for c in clips]
    partes, previo, desplazamiento, usadas = [], "[0:v]", 0.0, []
    for i in range(1, len(clips)):
        # El cruce arranca CRUCE segundos antes de que se acabe la toma
        # anterior, así que cada empalme se come ese pedacito del total.
        desplazamiento += duraciones[i - 1] - CRUCE
        cruce = TRANSICIONES[(arranque + i - 1) % len(TRANSICIONES)]
        usadas.append(cruce)
        etiqueta = f"[x{i}]"
        partes.append(
            f"{previo}[{i}:v]xfade=transition={cruce}:duration={CRUCE}:"
            f"offset={max(0.0, desplazamiento):.3f}{etiqueta}"
        )
        previo = etiqueta
    log(f"Cruces entre tomas: {', '.join(usadas)}.")
    _correr(
        ["ffmpeg", "-y", "-v", "error"] + entradas +
        ["-filter_complex", ";".join(partes), "-map", previo,
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-r", str(FPS), str(salida)],
        "unión de las tomas",
    )
    return salida


# ---------------------------------------------------------------------------
# Las tres franjas
# ---------------------------------------------------------------------------

def plan_de_tomas(cantidad_fotos, segundos):
    """Arma la lista de tomas: qué foto y con qué movimiento va cada una.

    La misma foto vuelve varias veces, pero nunca con el mismo movimiento dos
    veces seguidas. Son diez movimientos, o sea unos treinta segundos de video
    antes de que la rueda vuelva a empezar; en un reel largo alguno se repite,
    pero siempre con veinte segundos y varios cruces distintos de por medio, que
    es tiempo de sobra para que no se note.

    Además, cada reel arranca por un movimiento distinto, elegido a partir de
    cuánto dura. No es al azar (el bot tiene que dar siempre el mismo resultado
    con la misma entrada), pero como cada narración dura distinto, dos videos
    seguidos no empiezan nunca igual.
    """
    cantidad_fotos = max(1, cantidad_fotos)
    cuantas = max(1, int(round(segundos / SEGUNDOS_POR_TOMA)))
    dura = segundos / cuantas
    arranque = int(round(segundos * 10)) % len(MOVIMIENTOS)
    plan = []
    for i in range(cuantas):
        foto = i % cantidad_fotos
        movimiento = MOVIMIENTOS[(arranque + i) % len(MOVIMIENTOS)]
        plan.append((foto, movimiento, dura))
    return plan


def _franja_central(fotos, clip, segundos, tmp, centro_h=CENTRO_H):
    """La franja grande del medio: donde pasa la acción."""
    salida = tmp / "centro.mp4"
    if clip:
        filtro = (
            f"scale={LIENZO_W}:{centro_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={LIENZO_W}:{centro_h},setsar=1,fps={FPS},format=yuv420p"
        )
        _correr(
            ["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(clip),
             "-t", f"{segundos:.3f}", "-vf", filtro, "-an",
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", str(salida)],
            "franja central desde el clip",
        )
        return salida

    plan = plan_de_tomas(len(fotos), segundos)
    if CRUCE > 0 and len(plan) > 1:
        # Si se activa el cruce, cada toma tiene que durar un poco más para
        # compensar lo que se pierde en el solape.
        extra = CRUCE * (len(plan) - 1) / len(plan)
        plan = [(f, m, d + extra) for f, m, d in plan]

    piezas = []
    for i, (indice_foto, movimiento, dura) in enumerate(plan):
        piezas.append(
            _clip_de_foto(
                fotos[indice_foto], dura, LIENZO_W, centro_h,
                tmp / f"toma_{i:02d}.mp4", movimiento,
            )
        )
    log(f"Franja central: {len(piezas)} tomas de {plan[0][2]:.1f} s sobre {len(fotos)} foto(s).")
    log("Movimientos: " + ", ".join(m for _, m, _ in plan) + ".")
    return _encadenar(piezas, salida, tmp, arranque=int(round(segundos * 10)))


def _franja_fondo(fuente, es_clip, segundos, tmp):
    """El fondo de toda la pantalla: la misma imagen, ampliada y desenfocada."""
    salida = tmp / "fondo.mp4"
    filtro = (
        f"scale={LIENZO_W}:{LIENZO_H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={LIENZO_W}:{LIENZO_H},setsar=1,{DESENFOQUE},"
        f"fps={FPS},format=yuv420p"
    )
    if es_clip:
        entrada = ["-stream_loop", "-1", "-i", str(fuente)]
    else:
        entrada = ["-loop", "1", "-framerate", str(FPS), "-i", str(fuente)]
    _correr(
        ["ffmpeg", "-y", "-v", "error"] + entrada +
        ["-t", f"{segundos:.3f}", "-vf", filtro, "-an",
         "-c:v", "libx264", "-crf", "22", "-preset", "veryfast", str(salida)],
        "fondo desenfocado",
    )
    return salida


def _franja_pie(fuente, es_clip, segundos, tmp, pie_h=PIE_H):
    """La franja de abajo: un segundo plano desenfocado.

    Lleva el MISMO desenfoque que la franja de arriba (ver DESENFOQUE). Antes
    iba nítida y la imagen quedaba repetida dos veces a la vista, peleando con
    la del centro; borrosa, el ojo se va solo a la foto del medio y el reel se
    lee de un golpe. Conserva su movimiento propio, muy lento, para que la
    parte de abajo respire y no se vea como una foto pegada.
    """
    salida = tmp / "pie.mp4"
    if es_clip:
        filtro = (
            f"scale={LIENZO_W}:{pie_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={LIENZO_W}:{pie_h},setsar=1,{DESENFOQUE},fps={FPS},format=yuv420p"
        )
        _correr(
            ["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(fuente),
             "-t", f"{segundos:.3f}", "-vf", filtro, "-an",
             "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", str(salida)],
            "franja de abajo desde el clip",
        )
        return salida
    # Con foto fija: un vaivén lateral que, al repartirse en toda la duración
    # del reel, queda lento y no pelea con el movimiento rápido del centro. Va
    # más cerrado a propósito, para que se lea como otro encuadre y no como la
    # misma imagen repetida debajo.
    return _clip_de_foto(
        fuente, segundos, LIENZO_W, pie_h, salida, "pie_cerrado", acabado=DESENFOQUE
    )


# ---------------------------------------------------------------------------
# Banner (título + sticker), dibujado con Pillow
# ---------------------------------------------------------------------------

def _fuente_emoji(tamano=109):
    """Carga la fuente de emojis de color, si el sistema la tiene."""
    from PIL import ImageFont

    candidatas = [
        CARPETA_ASSETS / "NotoColorEmoji.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf"),
    ]
    for ruta in candidatas:
        if ruta.exists():
            try:
                return ImageFont.truetype(str(ruta), tamano)
            except Exception:
                continue
    return None


def _pegar_emojis(lienzo, emojis, centro_x, y, alto=86):
    """Dibuja la fila de emojis debajo del título. Si no se puede, no pasa nada."""
    from PIL import Image, ImageDraw

    fuente = _fuente_emoji()
    if not fuente or not emojis:
        return
    try:
        # La fuente de color solo existe en un tamaño fijo, así que se dibuja
        # grande y después se achica.
        # Se mide por caracteres, no por coincidencias: "😱🔥" es una sola
        # coincidencia pero dos dibujos, y si el lienzo se queda corto el
        # segundo emoji se corta.
        cuantos = max(1, sum(len(e) for e in emojis))
        capa = Image.new("RGBA", (150 * cuantos + 60, 170), (0, 0, 0, 0))
        d = ImageDraw.Draw(capa)
        d.text((20, 10), "".join(emojis), font=fuente, embedded_color=True)
        recorte = capa.crop(capa.getbbox() or (0, 0, 1, 1))
        escala = alto / max(1, recorte.height)
        recorte = recorte.resize(
            (max(1, int(recorte.width * escala)), alto), Image.LANCZOS
        )
        lienzo.alpha_composite(recorte, (int(centro_x - recorte.width / 2), int(y)))
    except Exception as e:  # la fuente de emojis es un lujo, no un requisito
        log(f"No se pudieron dibujar los emojis ({e}); sigo sin ellos.")


def _ajustar_fuente(dibujo, texto, ruta_fuente, ancho_max, inicial, minimo=44):
    """Busca el tamaño de letra más grande que entre en dos renglones."""
    from PIL import ImageFont

    tamano = inicial
    while tamano > minimo:
        f = ImageFont.truetype(str(ruta_fuente), tamano)
        renglones = _envolver(dibujo, texto, f, ancho_max)
        if len(renglones) <= 2 and all(
            dibujo.textlength(r, font=f) <= ancho_max for r in renglones
        ):
            return f, renglones
        tamano -= 3
    f = ImageFont.truetype(str(ruta_fuente), minimo)
    return f, _envolver(dibujo, texto, f, ancho_max)


def _envolver(dibujo, texto, fuente, ancho_max):
    palabras, renglones, actual = texto.split(), [], ""
    for p in palabras:
        prueba = (actual + " " + p).strip()
        if dibujo.textlength(prueba, font=fuente) <= ancho_max:
            actual = prueba
        else:
            if actual:
                renglones.append(actual)
            actual = p
    if actual:
        renglones.append(actual)
    return renglones or [""]


def banner_png(titulo, salida, sticker=None, banner_h=BANNER_H):
    """Dibuja el banner de arriba: sticker, título en amarillo y emojis.

    Sale un PNG transparente del ancho del lienzo, para superponerlo tal cual
    sobre el fondo desenfocado.
    """
    from PIL import Image, ImageDraw

    lienzo = Image.new("RGBA", (LIENZO_W, banner_h), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(lienzo)

    emojis = RE_EMOJI.findall(titulo or "")
    limpio = RE_EMOJI.sub("", titulo or "").strip()
    limpio = re.sub(r"\s{2,}", " ", limpio).upper()

    # El sticker va arriba, centrado, si hay uno.
    y_titulo = int(banner_h * 0.72)
    if sticker and Path(sticker).exists():
        try:
            img = Image.open(sticker).convert("RGBA")
            alto_sticker = int(banner_h * 0.52)
            escala = alto_sticker / img.height
            img = img.resize(
                (max(1, int(img.width * escala)), alto_sticker), Image.LANCZOS
            )
            lienzo.alpha_composite(img, ((LIENZO_W - img.width) // 2, int(banner_h * 0.06)))
        except Exception as e:
            log(f"No se pudo poner el sticker ({e}); sigo sin él.")

    # El título y la fila de emojis se tratan como un solo bloque y se apoyan
    # sobre el borde de abajo del banner. Así nunca se salen del recuadro,
    # tenga el banner el alto que tenga.
    alto_emojis = 84 if emojis else 0
    hueco_emojis = 16 if emojis else 0
    if limpio:
        fuente, renglones = _ajustar_fuente(
            dibujo, limpio, FUENTE_TITULO, LIENZO_W - 80, 92, 46
        )
        alto_renglon = fuente.size * 1.12
        alto_bloque = len(renglones) * alto_renglon + hueco_emojis + alto_emojis
        # Centro del bloque, sin que se pase ni por arriba ni por abajo.
        tope = max(alto_bloque / 2 + 10, min(y_titulo, banner_h - 18 - alto_bloque / 2))
        y = tope - alto_bloque / 2 + alto_renglon / 2
        for renglon in renglones:
            dibujo.text(
                (LIENZO_W // 2, y), renglon, font=fuente, fill=AMARILLO, anchor="mm",
                stroke_width=max(5, fuente.size // 11), stroke_fill=NEGRO,
            )
            y += alto_renglon
        y_emojis = y - alto_renglon / 2 + hueco_emojis
    else:
        y_emojis = banner_h - 18 - alto_emojis

    _pegar_emojis(lienzo, emojis, LIENZO_W / 2, y_emojis, alto_emojis or 84)

    salida = Path(salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    lienzo.save(salida)
    return salida


# ---------------------------------------------------------------------------
# Armado final
# ---------------------------------------------------------------------------

def armar(salida, fotos=None, clip=None, titulo="", audio=None, subtitulos=None,
          sticker=None, adorno=None, musica=None, segundos=None, tmpdir=None):
    """Arma el reel completo y devuelve la ruta del archivo.

    salida      -- dónde dejar el .mp4
    fotos       -- lista de rutas de imágenes (si no hay clip)
    clip        -- ruta de un video de origen (tiene prioridad sobre las fotos)
    titulo      -- el texto del banner de arriba
    audio       -- la voz en off, opcional
    subtitulos  -- ruta de un .ass, opcional
    sticker     -- PNG que va arriba, en el banner
    adorno      -- PNG fijo que va abajo a la izquierda de la franja central
    musica      -- pista de fondo, se mezcla bien bajita
    segundos    -- duración forzada; si no se pasa, manda el audio (tope 30 s)
    """
    fotos = [Path(f) for f in (fotos or []) if Path(f).exists()]
    clip = Path(clip) if clip and Path(clip).exists() else None
    if not fotos and not clip:
        raise ErrorDeVideo("No hay ni fotos ni clip para armar el video.")

    propio = tmpdir is None
    tmp = Path(tmpdir or tempfile.mkdtemp(prefix="reel_"))
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        # 1) Cuánto va a durar
        if segundos:
            total = float(segundos)
        elif audio and Path(audio).exists():
            total = duracion(audio) + 0.4   # un respiro al final
        elif clip:
            total = duracion(clip)
        else:
            total = 18.0
        total = max(DURACION_MIN, min(DURACION_MAX, total))
        log(f"Duración del reel: {total:.1f} s")

        # 2) De dónde sale cada franja
        hay_sticker = bool(sticker and Path(sticker).exists())
        banner_h, centro_h, pie_h = medidas(hay_sticker)
        fuente_fondo = clip or fotos[0]
        fuente_pie = clip or (fotos[-1] if len(fotos) > 1 else fotos[0])

        fondo = _franja_fondo(fuente_fondo, bool(clip), total, tmp)
        centro = _franja_central(fotos, clip, total, tmp, centro_h)
        pie = _franja_pie(fuente_pie, bool(clip), total, tmp, pie_h)
        banner = banner_png(titulo, tmp / "banner.png", sticker, banner_h)

        # 3) Se apilan las capas
        entradas = ["-i", str(fondo), "-i", str(centro), "-i", str(pie), "-i", str(banner)]
        indice = 4
        idx_adorno = None
        if adorno and Path(adorno).exists():
            entradas += ["-i", str(adorno)]
            idx_adorno = indice
            indice += 1
        idx_voz = None
        if audio and Path(audio).exists():
            entradas += ["-i", str(audio)]
            idx_voz = indice
            indice += 1
        idx_musica = None
        if musica and Path(musica).exists():
            entradas += ["-stream_loop", "-1", "-i", str(musica)]
            idx_musica = indice
            indice += 1

        pasos = [
            f"[0:v][1:v]overlay=0:{banner_h}[a]",
            f"[a][2:v]overlay=0:{banner_h + centro_h}[b]",
            "[b][3:v]overlay=0:0[c]",
        ]
        ultima = "[c]"
        if idx_adorno is not None:
            pasos.append(
                f"{ultima}[{idx_adorno}:v]overlay=36:{banner_h + centro_h - 300}[d]"
            )
            ultima = "[d]"
        if subtitulos and Path(subtitulos).exists():
            # Se copia con nombre simple y se corre ffmpeg dentro de la carpeta
            # temporal: así la ruta no lleva caracteres que el filtro tenga que
            # escapar, que es una fuente clásica de dolores de cabeza.
            shutil.copy(subtitulos, tmp / "subs.ass")
            pasos.append(f"{ultima}ass=subs.ass:fontsdir=fonts[v]")
            ultima = "[v]"
        else:
            pasos.append(f"{ultima}null[v]")
            ultima = "[v]"

        # 4) El audio
        mapas_audio = []
        if idx_voz is not None and idx_musica is not None:
            pasos.append(
                f"[{idx_musica}:a]volume=0.10,atrim=0:{total:.3f}[m];"
                f"[{idx_voz}:a][m]amix=inputs=2:duration=first:dropout_transition=0[au]"
            )
            mapas_audio = ["-map", "[au]"]
        elif idx_voz is not None:
            mapas_audio = ["-map", f"{idx_voz}:a"]
        elif idx_musica is not None:
            pasos.append(f"[{idx_musica}:a]volume=0.18,atrim=0:{total:.3f}[au]")
            mapas_audio = ["-map", "[au]"]

        # La carpeta de fuentes tiene que estar al alcance del filtro de
        # subtítulos, que corre con la carpeta temporal como directorio actual.
        destino_fuentes = tmp / "fonts"
        if not destino_fuentes.exists():
            try:
                destino_fuentes.mkdir()
                for f in (BASE_DIR / "fonts").glob("*.ttf"):
                    shutil.copy(f, destino_fuentes / f.name)
            except Exception as e:
                log(f"No se pudieron copiar las fuentes ({e}).")

        salida = Path(salida).resolve()
        salida.parent.mkdir(parents=True, exist_ok=True)
        orden = (
            ["ffmpeg", "-y", "-v", "error"] + entradas +
            ["-filter_complex", ";".join(pasos), "-map", ultima] + mapas_audio +
            ["-t", f"{total:.3f}",
             "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
             "-crf", "21", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-r", str(FPS), "-g", str(FPS * 2),
             "-movflags", "+faststart"]
        )
        if mapas_audio:
            orden += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
        else:
            orden += ["-an"]
        orden.append(str(salida))
        _correr(orden, "armado final del reel", cwd=str(tmp), timeout=900)

        log(f"Reel listo: {salida.name} ({salida.stat().st_size/1024/1024:.1f} MB, {duracion(salida):.1f} s)")
        return salida
    finally:
        if propio:
            shutil.rmtree(tmp, ignore_errors=True)


def datos(ruta):
    """Ficha rápida del video armado, para dejar en el registro y en el chat."""
    ruta = Path(ruta)
    info = {"archivo": ruta.name, "segundos": round(duracion(ruta), 2)}
    try:
        info["mb"] = round(ruta.stat().st_size / 1024 / 1024, 2)
    except Exception:
        info["mb"] = 0
    try:
        salida = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "json", str(ruta)],
            capture_output=True, text=True, timeout=60,
        ).stdout
        stream = json.loads(salida)["streams"][0]
        info["ancho"], info["alto"] = stream["width"], stream["height"]
    except Exception:
        pass
    return info


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("uso: video.py salida.mp4 foto1.jpg [foto2.jpg ...]")
        raise SystemExit(2)
    armar(sys.argv[1], fotos=sys.argv[2:], titulo="¡PRUEBA DE ARMADO! 😱👻")
