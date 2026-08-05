#!/usr/bin/env python3
"""Prueba suelta del reel completo, de punta a punta.

Hace exactamente lo mismo que hace el bot cuando le toca sacar un video, pero
sin publicar nada en ninguna página y sin gastar créditos de Claude: el título
y la narración vienen escritos acá, no se le piden a la inteligencia.

El recorrido es el de verdad:

    foto  ->  voz en off (VEXVIP)  ->  subtítulos karaoke  ->  video armado

De dónde sale la foto, por orden de preferencia:

    1. La dirección que se pase en FOTO_URL, si se pasa.
    2. La última foto publicada en la página 1 (solo se lee, no se toca nada).
    3. Una foto cualquiera de internet, por si no hay llave a mano.
    4. Una imagen inventada acá mismo, para que la prueba corra igual aunque
       no haya nada de lo anterior.

Uso:

    python tools/prueba_reel.py [salida.mp4]

Deja el .mp4 donde se le diga (por defecto /tmp/prueba_reel.mp4) y escribe al
final la ficha del archivo: cuánto pesa, cuánto dura y de qué tamaño es.
"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voz          # noqa: E402
import video        # noqa: E402
import subtitulos   # noqa: E402

GRAPH_VERSION = "v25.0"

# Lo que se va a decir y a mostrar. Es un texto de relleno, escrito con el
# mismo tono y el mismo largo que los que salen de verdad, para que la prueba
# se parezca a lo real: título corto con emojis, narración de unos 20 segundos.
TITULO = os.environ.get(
    "PRUEBA_TITULO", "¡SE DIJERON DE TODO EN VIVO! 😱🔥"
)
NARRACION = os.environ.get(
    "PRUEBA_NARRACION",
    # Escrito a propósito con la forma que ahora se le pide a la inteligencia:
    # escena, después lo que se dijeron uno por uno y en orden, y un cierre.
    # Nada de frases de relleno: quien lo escucha entiende la historia entera
    # sin haber leído nada.
    "Aldo y Fabio quedaron solos en el cuarto naranja después del "
    "posicionamiento. Aldo le reclamó que lo dejó solo en la placa. Fabio le "
    "contestó que él no le debe nada a nadie. Aldo le respondió que entonces ya "
    "no son equipo. Fabio se echó en la cama y le dijo que hiciera lo que "
    "quisiera. Ninguno de los dos volvió a hablarse esa noche. ¿Quién tuvo la "
    "razón?",
)


def log(msg):
    print(f"[prueba] {msg}", flush=True)


def _guardar(contenido, destino):
    destino = Path(destino)
    destino.write_bytes(contenido)
    log(f"Foto lista: {destino.name} ({len(contenido)/1024:.0f} KB)")
    return destino


def _bajar(url, destino):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return _guardar(r.content, destino)


def foto_de_pagina_uno(destino):
    """Última foto publicada en la página 1. Solo lectura, no cambia nada."""
    page_id = os.environ.get("PAGE_ID_MAIN", "").strip()
    token = os.environ.get("PAGE_TOKEN_MAIN", "").strip()
    if not page_id or not token:
        return None
    # Sin decir nada secreto: alcanza para saber si es la misma llave que usa
    # el bot o si por acá llegó otra distinta.
    log(f"Página 1: id termina en …{page_id[-4:]}, llave de largo {len(token)}.")
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/posts"
    r = requests.get(
        url,
        params={
            # Los mismos campos que pide el bot. Ojo: `subattachments` tiene que
            # ir con sus subcampos entre llaves; pedirlo pelado da error 400.
            "fields": "id,attachments{media_type,type,media,subattachments{media,type}}",
            "limit": 15,
            "access_token": token,
        },
        timeout=60,
    )
    if r.status_code >= 400:
        log(f"Graph contestó {r.status_code}: {(r.text or '')[:300]}")
    r.raise_for_status()
    for post in r.json().get("data", []):
        adjuntos = (post.get("attachments") or {}).get("data", [])
        if not adjuntos:
            continue
        a0 = adjuntos[0]
        tipo = (a0.get("media_type") or a0.get("type") or "").lower()
        # Igual que el bot: los posts que allá son video se saltan enteros.
        if tipo in ("video", "video_inline", "video_autoplay", "reel"):
            continue
        subs = (a0.get("subattachments") or {}).get("data", [])
        candidatos = subs or [a0]
        for c in candidatos:
            src = ((c.get("media") or {}).get("image") or {}).get("src")
            if src:
                return _bajar(src, destino)
    return None


def foto_inventada(destino):
    """Última red: una imagen armada acá, sin depender de nadie."""
    from PIL import Image, ImageDraw

    ancho, alto = 1440, 1080
    img = Image.new("RGB", (ancho, alto))
    dibujo = ImageDraw.Draw(img)
    # Degradado en diagonal: se ve claramente si el zoom y el paneo se mueven.
    for y in range(alto):
        for x in range(0, ancho, 8):
            t = (x / ancho + y / alto) / 2
            dibujo.rectangle(
                [x, y, x + 8, y + 1],
                fill=(int(30 + 200 * t), int(20 + 90 * t), int(90 + 140 * (1 - t))),
            )
    # Una cuadrícula encima, para notar el movimiento a simple vista.
    for x in range(0, ancho, 120):
        dibujo.line([(x, 0), (x, alto)], fill=(255, 255, 255), width=2)
    for y in range(0, alto, 120):
        dibujo.line([(0, y), (ancho, y)], fill=(255, 255, 255), width=2)
    img.save(destino, "JPEG", quality=90)
    log(f"Foto inventada: {Path(destino).name}")
    return Path(destino)


def conseguir_foto(destino):
    url = os.environ.get("FOTO_URL", "").strip()
    if url:
        try:
            log("Bajando la foto indicada en FOTO_URL.")
            return _bajar(url, destino)
        except Exception as e:
            log(f"No se pudo bajar esa foto ({e}).")
    try:
        foto = foto_de_pagina_uno(destino)
        if foto:
            log("Foto tomada de la página 1 (solo se leyó).")
            return foto
        log("Sin llave de la página 1, o sin fotos recientes allá.")
    except Exception as e:
        log(f"No se pudo mirar la página 1 ({e}).")
    try:
        log("Probando con una foto cualquiera de internet.")
        return _bajar("https://picsum.photos/1440/1080", destino)
    except Exception as e:
        log(f"Tampoco se pudo ({e}).")
    return foto_inventada(destino)


def main():
    salida = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/prueba_reel.mp4")
    trabajo = Path(tempfile.mkdtemp(prefix="prueba_reel_"))
    arranque = time.time()

    log("1/4 · Consiguiendo la foto.")
    foto = conseguir_foto(trabajo / "foto.jpg")

    log("2/4 · Pidiendo la voz en off.")
    audio = trabajo / "voz.mp3"
    ficha = voz.sintetizar(NARRACION, audio)
    log(
        f"Voz: {ficha['segundos']:.1f} s"
        + (f", {len(ficha['marcas'])} palabras con tiempo propio"
           if ficha.get("marcas") else ", sin tiempos por palabra")
    )
    if not ficha["segundos"]:
        raise SystemExit(
            "El audio mide 0 s: falta ffprobe (ffmpeg) en esta máquina."
        )

    log("3/4 · Escribiendo los subtítulos.")
    subs = trabajo / "subs.ass"
    subtitulos.escribir_ass(
        NARRACION, ficha["segundos"], subs,
        marcas=ficha.get("marcas"),
        margen_abajo=video.margen_subtitulos(False),
    )
    renglones = subs.read_text(encoding="utf-8").count("Dialogue:")
    log(f"Subtítulos: {renglones} renglones.")

    log("4/4 · Armando el video.")
    video.armar(
        salida,
        fotos=[str(foto)],
        titulo=TITULO,
        audio=str(audio),
        subtitulos=str(subs),
        tmpdir=str(trabajo / "armado"),
    )

    ficha_video = video.datos(salida)
    ficha_video["tardanza_seg"] = round(time.time() - arranque, 1)
    print(json.dumps(ficha_video, ensure_ascii=False))

    if ficha_video.get("alto") != 1920 or ficha_video.get("ancho") != 1080:
        raise SystemExit("El video no salió vertical de 1080x1920.")
    if ficha_video["segundos"] < 5:
        raise SystemExit("El video salió demasiado corto.")
    log("Todo en orden.")


if __name__ == "__main__":
    main()
