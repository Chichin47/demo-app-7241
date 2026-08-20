#!/usr/bin/env python3
"""
Poll & publish worker.

Cada ejecución:
1. Lee los últimos posts de la página principal (foto/álbum, ignora video/reel).
2. Descarta los ya procesados (state/processed_ids.json).
3. Para cada post nuevo, pide a Claude que elija las frases resaltantes,
   decida en qué foto va cada una, aplique la censura estilo "maquillaje"
   (números + tachado) a términos fuertes, y redacte una descripción alterna.
4. Compone la imagen final (compose_post.py) y la publica en la página
   de respaldo.
5. Marca el post como procesado.

Pensado para correr cada 5-10 min vía GitHub Actions (cron). Es idempotente:
si se interrumpe a mitad de camino, el peor caso es reprocesar un post
(el estado solo se marca "processed" tras publicar con éxito).
"""
import os
import re
import sys
import json
import time
import shutil
import tempfile
import subprocess
from pathlib import Path

import requests

import cola
import voz
import video
import insta
import subtitulos

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state" / "processed_ids.json"
PUBLISHED_MAP_PATH = BASE_DIR / "state" / "published_map.json"
PUBLISH_CLOCK_PATH = BASE_DIR / "state" / "publish_clock.json"
COMPOSE_SCRIPT = BASE_DIR / "compose_post.py"
GRAPH_VERSION = "v25.0"

PAGE_ID_MAIN = os.environ["PAGE_ID_MAIN"]
PAGE_TOKEN_MAIN = os.environ["PAGE_TOKEN_MAIN"]
PAGE_ID_BACKUP = os.environ["PAGE_ID_BACKUP"]
PAGE_TOKEN_BACKUP = os.environ["PAGE_TOKEN_BACKUP"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Llave de USUARIO, larga. Si está puesta, el bot se fabrica solo las llaves de
# las dos páginas cada vez que arranca, y las de los secretos pasan a ser un
# respaldo por si esta llamada falla.
#
# Esto existe por un problema que costó caro. Las llaves de página que da el
# Explorador de la API son de SESIÓN: duran un par de horas y después el bot se
# cae con "Session has expired" (código 190, subcódigo 463). Las que se piden
# con /me/accounts usando una llave de usuario larga, en cambio, no vencen.
#
# Pedirlas acá tiene dos ventajas sobre pegarlas a mano: nunca se copia una
# corta por error, y cuando toque renovar hay UNA sola llave que cambiar en vez
# de tres. Cuesta una llamada por arranque, que al lado de todo lo demás no es
# nada.
USER_TOKEN = os.environ.get("USER_TOKEN", "").strip()

# Opcionales: si están configurados, las previews de DRY_RUN se mandan a Telegram
# en vez de (además de) quedar solo como artifact de GitHub Actions.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

def env_num(nombre, por_defecto, tipo=float):
    """Lee una variable numérica del entorno tolerando que venga vacía.

    GitHub Actions define las variables de repositorio como cadena vacía cuando
    no existen, así que int("")/float("") reventaría el paso entero.
    """
    bruto = (os.environ.get(nombre) or "").strip()
    if not bruto:
        return tipo(por_defecto)
    try:
        return tipo(bruto)
    except ValueError:
        print(f"[app] Valor inválido en {nombre}={bruto!r}; se usa {por_defecto}.", flush=True)
        return tipo(por_defecto)


MAX_POSTS_PER_RUN = env_num("MAX_POSTS_PER_RUN", 5, int)
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Anti-"todo de golpe": nunca se publica más de UN post por corrida, y además
# tienen que haber pasado al menos MIN_MINUTES_BETWEEN_POSTS minutos desde la
# publicación anterior (sea automática o manual por Telegram). Si se acumularon
# varios posts pendientes, salen de a uno espaciados en vez de en ráfaga, que es
# justo el patrón que Meta marca como comportamiento de bot.
MIN_MINUTES_BETWEEN_POSTS = env_num("MIN_MINUTES_BETWEEN_POSTS", 5, float)

# Cuántos posts se piden por página al buscar hacia atrás, y cuántas páginas
# como máximo se recorren en una sola corrida. Esto existe para que, si el
# cron se atrasa (o el bot estuvo apagado un rato) y se acumulan más de
# `limit` posts nuevos en la página principal, el bot igual pueda "ver" los
# más viejos en vez de que se le escapen por quedar fuera de la primera
# página de resultados.
FETCH_PAGE_SIZE = env_num("FETCH_PAGE_SIZE", 25, int)
FETCH_MAX_PAGES = env_num("FETCH_MAX_PAGES", 5, int)


def log(msg):
    print(f"[app] {msg}", flush=True)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"processed": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def load_publish_clock():
    if PUBLISH_CLOCK_PATH.exists():
        try:
            return json.loads(PUBLISH_CLOCK_PATH.read_text())
        except Exception:
            return {}
    return {}


def mark_published_now(origen="auto"):
    """Anota que se acaba de publicar algo, para espaciar lo siguiente."""
    try:
        PUBLISH_CLOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        PUBLISH_CLOCK_PATH.write_text(json.dumps({
            "last_publish_ts": int(time.time()),
            "origen": origen,
        }, indent=2))
    except Exception as e:
        log(f"No se pudo guardar el reloj de publicación: {e}")


def minutes_since_last_publish():
    ts = load_publish_clock().get("last_publish_ts")
    if not ts:
        return None
    return (time.time() - float(ts)) / 60.0


def can_publish_now():
    """True si ya pasó el tiempo mínimo desde la última publicación."""
    mins = minutes_since_last_publish()
    if mins is None:
        return True
    return mins >= MIN_MINUTES_BETWEEN_POSTS


def graph_get(path, token, **params):
    params["access_token"] = token
    r = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/{path}", params=params, timeout=30)
    if r.status_code >= 400:
        # Sin esto, el traceback solo dice "400 Bad Request" y no se sabe si es
        # el token, el permiso o el id. El cuerpo de la respuesta sí lo dice.
        detalle = (r.text or "")[:800]
        log(f"Graph respondió {r.status_code} en /{path}: {detalle}")
    r.raise_for_status()
    return r.json()


def _llaves_de_las_paginas():
    """Le pide a Meta la llave de cada página, a partir de la de usuario.

    Devuelve {id_de_pagina: llave} o {} si no se pudo. Nunca revienta: si esto
    falla, el bot sigue con las llaves de los secretos, que es como funcionaba
    antes. Peor caso, se queda igual que estaba; nunca peor.
    """
    if not USER_TOKEN:
        return {}
    try:
        r = requests.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/me/accounts",
            params={"fields": "id,name,access_token", "limit": 100,
                    "access_token": USER_TOKEN},
            timeout=30,
        )
        if r.status_code >= 400:
            log(f"No pude pedir las llaves de las páginas ({r.status_code}): "
                f"{(r.text or '')[:300]}")
            return {}
        return {d["id"]: d["access_token"]
                for d in (r.json().get("data") or [])
                if d.get("id") and d.get("access_token")}
    except Exception as e:
        log(f"No pude pedir las llaves de las páginas ({e}).")
        return {}


def _usar_llaves_frescas():
    """Reemplaza las llaves de las dos páginas por las que acaba de dar Meta."""
    global PAGE_TOKEN_MAIN, PAGE_TOKEN_BACKUP
    llaves = _llaves_de_las_paginas()
    if not llaves:
        if USER_TOKEN:
            log("Sigo con las llaves de los secretos.")
        return
    cuales = []
    if llaves.get(PAGE_ID_MAIN):
        PAGE_TOKEN_MAIN = llaves[PAGE_ID_MAIN]
        cuales.append("página 1")
    if llaves.get(PAGE_ID_BACKUP):
        PAGE_TOKEN_BACKUP = llaves[PAGE_ID_BACKUP]
        cuales.append("página 2")
    faltan = [p for p, n in ((PAGE_ID_MAIN, "página 1"), (PAGE_ID_BACKUP, "página 2"))
              if not llaves.get(p)]
    if cuales:
        log(f"Llaves frescas pedidas a Meta para: {', '.join(cuales)}.")
    if faltan:
        log(f"Ojo: la llave de usuario no da acceso a {len(faltan)} de las dos "
            f"páginas; para esa(s) se usa la del secreto.")


_usar_llaves_frescas()


def fetch_recent_posts(processed=None):
    """Trae los posts recientes de la página principal.

    Si `processed` es None (ej. TEST_MODE), trae solo una página (comportamiento
    simple, no necesita mirar atrás).

    Si `processed` es un set de IDs ya procesados, va paginando hacia atrás
    (usando el cursor `paging.next` de Graph API) hasta encontrar una página
    que contenga al menos un post ya conocido (ahí se sabe que ya se llegó al
    territorio ya revisado y no hace falta seguir), o hasta un tope de
    `FETCH_MAX_PAGES` páginas como salvaguarda. Así, si el bot estuvo un rato
    sin correr (cron atrasado, GitHub Actions caído, etc.) y se acumularon más
    posts nuevos de los que caben en una sola página, igual los detecta todos
    en vez de que los más viejos queden fuera de la vista.
    """
    fields = "id,message,created_time,attachments{media_type,type,url,media,subattachments{media,type,url}}"

    if processed is None:
        data = graph_get(f"{PAGE_ID_MAIN}/posts", PAGE_TOKEN_MAIN, fields=fields, limit=FETCH_PAGE_SIZE)
        return data.get("data", [])

    all_posts = []
    next_url = None
    for _ in range(FETCH_MAX_PAGES):
        if next_url:
            r = requests.get(next_url, timeout=30)
            r.raise_for_status()
            data = r.json()
        else:
            data = graph_get(f"{PAGE_ID_MAIN}/posts", PAGE_TOKEN_MAIN, fields=fields, limit=FETCH_PAGE_SIZE)
        posts = data.get("data", [])
        if not posts:
            break
        all_posts.extend(posts)
        if any(p["id"] in processed for p in posts):
            break  # ya llegamos a territorio conocido, no hace falta seguir
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
    return all_posts


def classify_attachment(post):
    """Devuelve ('photo', [image_urls]) o ('video', None) o ('other', None)."""
    att = post.get("attachments", {}).get("data", [])
    if not att:
        return "other", None
    a0 = att[0]
    media_type = a0.get("media_type", a0.get("type", ""))
    if media_type in ("video", "video_inline", "video_autoplay", "reel"):
        return "video", None
    images = []
    subs = a0.get("subattachments", {}).get("data", [])
    if subs:
        for s in subs:
            url = s.get("media", {}).get("image", {}).get("src")
            if url:
                images.append(url)
    else:
        url = a0.get("media", {}).get("image", {}).get("src")
        if url:
            images.append(url)
    if not images:
        return "other", None
    return "photo", images[:3]


def resumen_para_cola(post):
    """Convierte un post pendiente en la ficha corta que muestra el panel.

    No pide nada nuevo a Facebook ni a Claude: usa lo que ya se trajo en este
    mismo barrido. Por eso mirar la cola no cuesta ni un token.
    """
    kind, imgs = classify_attachment(post)
    imgs = imgs or []
    texto = (post.get("message") or "").strip()
    if kind == "video":
        estado = "video"
    elif kind != "photo" or not imgs:
        estado = "sin_foto"
    elif not texto:
        estado = "sin_texto"
    else:
        estado = "listo"
    return {
        "id": post["id"],
        "clave": cola.clave(post["id"]),
        "created_time": post.get("created_time", ""),
        "texto": texto[:cola.MAX_TEXTO],
        "foto": imgs[0] if imgs else "",
        "n_fotos": len(imgs),
        "estado": estado,
    }


def download_image(url, dest):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    dest.write_bytes(r.content)


NOMBRE_ALTERNA = os.environ.get("PAGE_NAME_BACKUP", "").strip() or "la página alterna"
NOMBRE_PRINCIPAL = os.environ.get("PAGE_NAME_MAIN", "").strip() or "la página principal"

_PEDIDO_BASE = f"""Eres el editor de la página alterna "{NOMBRE_ALTERNA}", que reposta \
contenido de la página principal "{NOMBRE_PRINCIPAL}" con un formato distinto para evitar \
duplicado de contenido en Facebook.

Tu trabajo, dado el texto original de un post (título + diálogo) y la cantidad de fotos disponibles:

1. Elige 1-2 frases cortas y resaltantes del diálogo (máximo ~70 caracteres cada una) que enganchen \
   al lector, conservando la atribución al personaje si la hay (ej: "Fabio: Es un juego!").
2. Decide en qué foto (índice 0, 1 o 2) va cada frase, según el orden lógico del relato.
3. Aplica censura tipo "maquillaje" a insultos fuertes, términos discriminatorios (racismo, \
   homofobia, xenofobia, etc.) e improperios: reemplaza vocales por números similares visualmente \
   (a→4, e→3, i→1, o→0) y envuelve SOLO esa palabra entre virgulillas ~así~ (esto activa una línea \
   de tachado sobre la palabra en la imagen, pero se sigue leyendo). Ejemplos: racismo→~r4c1smo~, \
   discriminación→~d1scr1m1nac10n~, idiota→~1d10t4~, puta→~put4~, maricón→~m4r1c0n~. NO censures \
   palabras neutras.
4. Redacta la descripción alterna del post con esta estructura, en este orden:

   a) PREÁMBULO: la reseña corta que va arriba de todo, para que el que pasa scrolleando \
      entienda de qué va antes de leer el diálogo. Se escribe con palabras DISTINTAS al post \
      original (tono enganchador), para que Facebook no lo marque como contenido duplicado.
      - Si el post TRAE diálogo: UNA sola oración, de 18 a 28 palabras. Dos solo si la primera \
        de verdad no alcanza, y aun así el preámbulo entero nunca pasa de 200 caracteres. La \
        razón es concreta: Facebook corta la descripción con "Ver más" a las pocas líneas, y si \
        el preámbulo se come ese espacio, el diálogo —que es lo que engancha— queda escondido. \
        El preámbulo contextualiza; el diálogo es el que vende.
      - Si el post NO trae diálogo: ahí el preámbulo es todo lo que hay, así que puede ir de 2 a \
        3 oraciones y contar la historia completa.

   b) DIÁLOGO: si el texto original trae diálogo (líneas tipo "Nombre: frase", o con guiones, o \
      cualquier intervención hablada de un personaje), va SIEMPRE debajo del preámbulo, porque el \
      diálogo es el contexto y sin él la gente no entiende la publicación. Reglas del diálogo:
      - Una línea por intervención, empezando con guion y el nombre: "- Aldo: lo que dijo".
      - Respeta el orden y el sentido de cada intervención. Puedes arreglar ortografía, tildes y \
        puntuación, pero NO cambies lo que dice el personaje, NO lo resumas hasta que pierda la \
        gracia y NO inventes intervenciones que no estén en el original.
      - Separa cada línea con un salto de línea real.
      - Máximo 6 intervenciones: si el original trae más, quédate con las que sostienen el chiste \
        o el conflicto y descarta el relleno.
      - Si el original NO trae diálogo, sáltate esta parte: solo preámbulo y hashtags.

   c) CIERRE: 1-2 hashtags relevantes en la última línea.

   Aplica la misma censura tipo "maquillaje" a términos fuertes en TODA la descripción, preámbulo \
   y diálogo incluidos (aquí sin virgulillas, solo el reemplazo con números).
5. Si el post no tiene contenido de diálogo aprovechable (por ejemplo solo es un anuncio, o el texto \
   está vacío), responde skip=true con skip_reason.
"""

# Todo esto se le pide SOLO cuando el post de verdad va a salir en video. Está
# aparte porque es más de la mitad del pedido, y el formato ya se sabe antes de
# llamar a Claude: lo decide la marca #UR o el botón 🎬 de la cola. Pedir el
# guion narrado en un post que va a salir en foto es pagar por un texto que
# nadie va a leer nunca; de cada cinco publicaciones, cuatro son foto.
_PEDIDO_VIDEO = """
6. Además de lo anterior, prepara el material por si este post sale en formato video corto (reel). \
   Son dos campos y SIEMPRE se llenan cuando skip=false:

   a) titulo_reel: el letrero grande que va arriba del video. Máximo 45 caracteres, en mayúsculas, \
      de dos a seis palabras, tipo gancho ("SE LE SALIÓ TODO EN VIVO"). Puede terminar con uno o dos \
      emojis. Aquí SÍ va la censura tipo maquillaje (vocales por números, sin virgulillas), porque \
      este texto se dibuja, no se lee en voz alta.

   b) narracion: lo que dice la voz en off. El largo lo decide la historia, no un objetivo de \
      duración: usa las palabras que hagan falta para que entren la escena, todas las \
      intervenciones y el remate, y ni una más. En la práctica lo normal son 60 a 90 palabras. \
      El techo duro son 125, y llegar ahí tiene que ser la excepción: un post con siete u ocho \
      intervenciones largas. Si la historia cerró en 55 palabras, el guion termina en 55 y listo. \
      Está PROHIBIDO estirar con relleno para llenar tiempo: un video de 22 segundos que se \
      entiende entero es mejor que uno de 45 con paja adentro.

      Lo más importante de todo: quien escucha el video NO ve la descripción escrita y muchas veces \
      tampoco conoce el programa. La narración tiene que bastarse sola. Al terminar de escucharla, \
      alguien que nunca vio nada de esto tiene que haber entendido la historia completa: quién, \
      dónde, qué pasó exactamente, qué se dijeron y cómo quedó la cosa. Si el que escucha se queda \
      preguntando "¿de qué están hablando?", el guion está mal hecho.

      Cómo se arma, en este orden:
      - ARRANQUE (1 oración, a veces 2): es lo que decide si alguien se queda mirando, así que \
        no puede sonar igual en todos los videos. Al final del pedido te digo con qué forma abrir \
        este en concreto (la escena, la acción, un detalle, una frase, la reacción o el contraste) \
        y con qué aperturas ya se usaron, para que no repitas. Sea cual sea la forma: la primera \
        oración lleva un dato concreto de ESTE post —nunca un gancho vacío que serviría para \
        cualquiera— y como muy tarde en la segunda oración ya quedó claro quiénes son y dónde \
        están. Lo que cambia es el orden en que entra el contexto, no que el contexto desaparezca.
      - CUERPO (el grueso): cuenta lo que pasó siguiendo el diálogo original, intervención por \
        intervención y en el mismo orden, pasado a habla natural: "Aldo le reclamó que ya no lo \
        aguantaba, y Fabio le contestó que para él todo era un juego". No te saltes intervenciones \
        que cambien el sentido, no las inviertas de orden y no inventes ninguna que no esté. Si el \
        post no trae diálogo, el cuerpo cuenta con detalle lo que sí dice el original.
      - CIERRE: acá va SIEMPRE la última intervención del diálogo, la que remata. Es la parte \
        más importante del guion y la que más se cae: el video no puede terminar a mitad del \
        ida y vuelta. Si el original cierra con una respuesta filosa, una burla o una reacción \
        (alguien que se queda callado, que se sonroja, que se ríe), eso se cuenta sí o sí, y \
        recién después, si sobra lugar, va una pregunta que invite a opinar.

      Y una regla de reparto que manda sobre todas: el remate NO se negocia. Si te estás \
      quedando sin palabras, achicá el arranque y resumí las intervenciones del medio, pero el \
      final se cuenta entero. Un guion que se corta antes del golpe está mal hecho aunque todo \
      lo anterior esté perfecto.

      Y estas reglas de forma:
      - TODO lo que digas tiene que salir del texto original. Prohibido el relleno inventado tipo \
        "las redes explotaron", "nadie se lo esperaba" o "esto se salió de control" cuando el post \
        no dice nada de eso: son palabras gastadas que ocupan el lugar del contexto que sí importa.
      - NADA de hashtags, ni emojis, ni "Nombre:" delante de las frases, ni acotaciones entre \
        paréntesis: todo eso se escucha mal. Si citas a alguien, dilo natural: "Fabio le respondió \
        que para él todo es un juego".
      - Oraciones cortas, de menos de 15 palabras, y puntuación real: la voz respira en los puntos y \
        las comas, y de ahí salen los subtítulos.
      - En la narración NO se usa la censura de números: se leería como galimatías. En su lugar, \
        esquiva la palabra fuerte diciendo lo mismo de otro modo ("lo insultó", "le dijo de todo", \
        "se le fue encima con un comentario racista"). Nunca escribas el insulto tal cual.
      - Números y siglas escritos como se pronuncian ("veinticuatro siete", no "24/7").
      - El titulo_reel y la narración tienen que hablar de lo mismo: el letrero de arriba es el \
        anzuelo de la historia que se va a contar, no un titular suelto.
"""

_PEDIDO_CIERRE = """
Responde ÚNICAMENTE llamando a la herramienta submit_edit con el JSON estructurado."""


# De qué programa es el post. Esto va SOLO cuando no es el reality de siempre,
# y es corto a propósito: Sonnet ya sabe qué es Top Chef, no hace falta
# explicárselo ni darle un molde nuevo. Lo único que le falta es saber CUÁL es,
# y le falta por culpa nuestra: la marca #topchefvip5 se borra del texto antes
# de que él lo vea, así que sin esta línea lee un diálogo suelto y lo escribe
# como si fuera La Casa de los Famosos, que es lo que ve todos los días.
#
# Son unas 60 palabras y solo viajan en los posts de ese programa; en los demás
# el pedido queda exactamente igual que antes, sin un token de más.
PROGRAMAS = {
    "topchef": {
        "contexto": (
            "\nEL PROGRAMA: este post es de Top Chef VIP 5, el reality de cocina "
            "de Telemundo, en español. NO es La Casa de los Famosos y no se le "
            "parece en nada: acá se "
            "compite cocinando, con retos, jurado y eliminaciones, no con placas "
            "ni nominaciones. Escribí con el tono y el vocabulario de Top Chef y "
            "elegí hashtags de Top Chef; no metas nada de La Casa de los Famosos.\n"
        ),
        "hashtag": "#TopChefVIP5",
    },
}


def prompt_sistema(con_video, programa=None):
    """El pedido que se le manda a Claude, con o sin la parte del video."""
    extra = (PROGRAMAS.get(programa) or {}).get("contexto", "")
    return (_PEDIDO_BASE + (_PEDIDO_VIDEO if con_video else "") + extra
            + _PEDIDO_CIERRE)


# Se deja armado el completo por si algo de afuera lo importa por nombre.
CLAUDE_SYSTEM_PROMPT = prompt_sistema(True)

SUBMIT_TOOL = {
    "name": "submit_edit",
    "description": "Envía la edición estructurada del post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "skip": {"type": "boolean"},
            "skip_reason": {"type": "string"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "image_index": {"type": "integer"},
                        "text": {"type": "string"},
                        "color": {"type": "string", "enum": ["orange", "yellow", "white", "red"]},
                    },
                    "required": ["image_index", "text", "color"],
                },
            },
            "caption": {
                "type": "string",
                "description": (
                    "Descripción alterna completa: preámbulo reescrito (una sola "
                    "oración de 18 a 28 palabras cuando el post trae diálogo, sin "
                    "pasar de 200 caracteres; más largo solo si no hay diálogo), "
                    "luego el diálogo del original (una línea por intervención, con "
                    "guion y nombre, separadas por saltos de línea reales) si lo "
                    "hubiera, y al final los hashtags."
                ),
            },
            "titulo_reel": {
                "type": "string",
                "description": (
                    "Letrero grande del video, máximo 45 caracteres, en mayúsculas, "
                    "con censura de maquillaje si hace falta y uno o dos emojis "
                    "opcionales al final."
                ),
            },
            "narracion": {
                "type": "string",
                "description": (
                    "Guion hablado para la voz en off: normalmente 60 a 90 "
                    "palabras y como mucho 125 (solo si la historia de verdad "
                    "las necesita; nunca relleno para llenar tiempo), prosa "
                    "corrida en tercera persona. Tiene que contar la historia "
                    "COMPLETA y entenderse sola, sin leer la descripción: "
                    "escena, lo que se dijeron intervención por intervención en "
                    "el orden original, y el remate final del diálogo, que es "
                    "obligatorio y nunca se recorta. Todo sacado del post, sin "
                    "relleno inventado. Oraciones cortas, sin hashtags, sin "
                    "emojis, sin nombres con dos puntos y sin palabras "
                    "censuradas con números."
                ),
            },
        },
        "required": ["skip"],
    },
}

# Los campos que solo sirven para el video. Si el post va a salir en foto no se
# le ofrecen a Claude: lo que no está en la herramienta no se puede llenar, y lo
# que no se llena no se paga.
CAMPOS_DE_VIDEO = ("titulo_reel", "narracion")


def herramienta(con_video):
    """La misma herramienta de siempre, sin los campos del video cuando no toca."""
    if con_video:
        return SUBMIT_TOOL
    propiedades = {k: v for k, v in SUBMIT_TOOL["input_schema"]["properties"].items()
                   if k not in CAMPOS_DE_VIDEO}
    esquema = dict(SUBMIT_TOOL["input_schema"], properties=propiedades)
    return dict(SUBMIT_TOOL, input_schema=esquema)


MANUAL_OVERRIDE = (
    "\n\nIMPORTANTE: este post fue enviado A MANO por el administrador de la página, "
    "que ya decidió que se va a publicar. NO respondas skip=true bajo ninguna "
    "circunstancia: la regla 5 no aplica aquí. Aunque el texto no traiga diálogo de "
    "personajes (sea un anuncio, una pregunta, un comentario o una descripción "
    "cualquiera), igual tienes que:\n"
    "- elegir 1-2 frases cortas y llamativas para poner sobre la(s) foto(s); si no hay "
    "diálogo, resume la idea principal en una frase gancho de máximo ~70 caracteres;\n"
    "- aplicar la censura tipo maquillaje donde corresponda;\n"
    "- redactar la descripción alterna con la estructura de la regla 4: preámbulo "
    "reescrito, luego el diálogo si el texto trae diálogo, y los hashtags al final.\n"
    "- llenar igual titulo_reel y narracion como dice la regla 6, porque el "
    "administrador puede pedir que este mismo post salga en video.\n"
    "Siempre devuelve skip=false con lines, caption, titulo_reel y narracion completos."
)

APARTE_OVERRIDE = (
    "\n\nIMPORTANTE: este post lleva la marca de apartado: no se publica en "
    "automático, se prepara para que el administrador lo suba A MANO a otra "
    "página. NO respondas skip=true bajo ninguna circunstancia: la regla 5 no "
    "aplica aquí. Aunque el texto sea un anuncio, un resultado, una pregunta o "
    "una descripción sin diálogo, igual tienes que:\n"
    "- elegir 1-2 frases cortas y llamativas para poner sobre la(s) foto(s); si "
    "no hay diálogo, resume la idea principal en una frase gancho de máximo "
    "~70 caracteres;\n"
    "- aplicar la censura tipo maquillaje donde corresponda;\n"
    "- redactar la descripción alterna con la estructura de la regla 4 (si no "
    "hay diálogo, solo el preámbulo reescrito y los hashtags);\n"
    "- si la herramienta ofrece titulo_reel y narracion, llénalos igual.\n"
    "Siempre devuelve skip=false con lines y caption completos."
)


# ---------------------------------------------------------------------------
# Cómo arranca la narración
# ---------------------------------------------------------------------------

# Cada post se le pide a Claude en una llamada nueva y sin memoria de las
# anteriores. Eso es bueno para que la reseña no se vaya estirando sola, pero
# tiene un costo: sin nada que lo empuje, Claude arranca siempre igual, y todos
# los videos terminaban abriendo con "En la casa de los famosos México...". Los
# primeros tres segundos son los que deciden si alguien se queda mirando, así
# que arrancar los diez videos con la misma frase es tirar esos tres segundos.
#
# La solución es de este lado: se lleva la cuenta de los videos publicados y a
# cada uno le toca una forma distinta de abrir, más las últimas aperturas que ya
# se usaron para que no repita ni la idea. La escena NO desaparece: lo que
# cambia es el orden, el contexto pasa a la segunda oración.
ARRANQUES_PATH = BASE_DIR / "state" / "arranques.json"

MAX_ARRANQUES_RECIENTES = 6

MODOS_ARRANQUE = [
    ("escena",
     "Abrí plantando la escena: quiénes están y dónde, con datos del post. "
     "Es la apertura más directa; usala tal cual."),
    ("accion",
     "Abrí con la ACCIÓN concreta que desató todo: lo que alguien hizo, no "
     "dónde estaban. El lugar y los nombres entran en la segunda oración."),
    ("detalle",
     "Abrí con el DETALLE concreto de la escena: el objeto, la comida, la "
     "prueba, el cuarto, lo que se ve en la foto. Recién después contás "
     "quiénes son y dónde están."),
    ("frase",
     "Abrí con algo que alguien DIJO, contado natural y sin comillas. Tiene "
     "que ser una intervención del medio, nunca la del remate: si abrís con "
     "el remate, el video ya no tiene final."),
    ("reaccion",
     "Abrí con la REACCIÓN: cómo quedaron los demás, quién se quedó callado, "
     "quién se rió. Después explicás qué la provocó."),
    ("contraste",
     "Abrí con el CONTRASTE de la situación: lo que se suponía que pasaba "
     "contra lo que terminó pasando, siempre con datos reales del post."),
]

REGLAS_ARRANQUE = (
    "Reglas del arranque, valen para cualquiera de las formas:\n"
    "- La primera oración tiene que llevar un dato concreto de ESTE post. Está "
    "prohibido abrir con una frase que serviría para cualquier otro "
    "(\"nadie se lo esperaba\", \"esto se salió de control\", \"lo que pasó te va "
    "a sorprender\"): son palabras vacías que gastan los segundos que más valen.\n"
    "- Como muy tarde en la segunda oración tiene que quedar claro quiénes son y "
    "dónde están. El que escucha no conoce el programa. Lo que cambia es el "
    "ORDEN, el contexto no se elimina nunca.\n"
    "- El arranque no adelanta el remate. El final se cuenta al final."
)


def _leer_arranques():
    try:
        datos = json.loads(ARRANQUES_PATH.read_text(encoding="utf-8"))
        if isinstance(datos, dict):
            return datos
    except Exception:
        pass
    return {}


def _primera_oracion(texto):
    """La apertura de un guion: hasta el primer punto, sin pasarse de 120."""
    limpio = " ".join((texto or "").split())
    if not limpio:
        return ""
    corte = limpio.find(". ")
    if corte > 0:
        limpio = limpio[:corte + 1]
    return limpio[:120]


def instruccion_arranque():
    """El bloque que se le suma al pedido para que no abra siempre igual.

    Se apoya en la cuenta de videos publicados, no en la de posts: los posts que
    salen como foto no se escuchan, así que no gastan turno. Si el archivo no
    existe todavía —primer video— toca el modo escena, que es exactamente lo que
    se venía haciendo.
    """
    datos = _leer_arranques()
    n = int(datos.get("n", 0) or 0)
    nombre, indicacion = MODOS_ARRANQUE[n % len(MODOS_ARRANQUE)]

    partes = [
        "\n\nCÓMO ABRIR LA NARRACIÓN EN ESTE POST\n"
        f"Forma que te toca: {nombre}. {indicacion}\n" + REGLAS_ARRANQUE
    ]
    recientes = [a for a in (datos.get("recientes") or []) if a][:MAX_ARRANQUES_RECIENTES]
    if recientes:
        lista = "\n".join(f"  · {a}" for a in recientes)
        partes.append(
            "\nAsí abrieron los últimos videos. No repitas ninguna, ni la frase "
            "ni la idea, ni empieces con las mismas palabras:\n" + lista
        )
    return "".join(partes)


def _anotar_arranque(narracion):
    """Guarda cómo abrió este video y pasa el turno al modo siguiente.

    Se llama solo cuando el video se publicó de verdad. Si se llamara en cada
    post, los que salen como foto irían corriendo la rueda sin que nadie los
    escuche, y la variedad que se ve en la página sería menos de la que dice la
    cuenta.
    """
    datos = _leer_arranques()
    datos["n"] = int(datos.get("n", 0) or 0) + 1
    apertura = _primera_oracion(narracion)
    if apertura:
        recientes = [a for a in (datos.get("recientes") or []) if a and a != apertura]
        datos["recientes"] = ([apertura] + recientes)[:MAX_ARRANQUES_RECIENTES]
    datos["ultimo_modo"] = MODOS_ARRANQUE[(datos["n"] - 1) % len(MODOS_ARRANQUE)][0]
    try:
        ARRANQUES_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARRANQUES_PATH.write_text(json.dumps(datos, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    except Exception as e:
        log(f"No se pudo anotar el arranque ({e}); sigo igual.")


def ask_claude(original_text, num_images, manual=False, con_video=False,
               programa=None, aparte=False):
    """Le pide a Claude la edición del post.

    con_video=True agrega la parte del pedido que explica cómo escribir el
    letrero y el guion hablado. Cuesta más o menos el doble, así que se manda
    solo cuando ya se sabe que ese post sale en video (lo decide la marca #UR o
    el botón 🎬 de la cola, y las dos cosas se saben antes de llamar).

    programa dice de qué reality es el post cuando no es el de siempre. Son dos
    líneas más y solo viajan en esos posts; con programa=None el pedido es
    idéntico al de toda la vida.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_msg = (
        f"Texto original del post:\n---\n{original_text}\n---\n"
        f"Cantidad de fotos disponibles: {num_images} (índices 0"
        + (f" a {num_images - 1}" if num_images > 1 else "") + ")."
    )
    if manual:
        user_msg += MANUAL_OVERRIDE
    elif aparte:
        user_msg += APARTE_OVERRIDE
    # Va en el mensaje y no en el prompt de sistema porque cambia post a post:
    # es lo único de todo el pedido que depende de lo que ya se publicó antes.
    # Solo sirve para variar el arranque de la narración, así que en los posts
    # que salen en foto no hace falta mandarla.
    if con_video:
        try:
            user_msg += instruccion_arranque()
        except Exception as e:
            log(f"No se pudo armar la indicación de arranque ({e}); sigo con la de siempre.")
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=prompt_sistema(con_video, programa),
        tools=[herramienta(con_video)],
        tool_choice={"type": "tool", "name": "submit_edit"},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_edit":
            return block.input
    raise RuntimeError("Claude no devolvió submit_edit")


# ---------------------------------------------------------------------------
# Freno de la reseña
# ---------------------------------------------------------------------------

# Hasta acá puede llegar el preámbulo (la reseña de arriba) cuando el post trae
# diálogo. Doscientos caracteres es más o menos una línea y media en el celular,
# que es justo lo que se ve antes del "Ver más" de Facebook.
LIMITE_PREAMBULO = 200

# Cómo se reconoce una línea de diálogo: empieza con guion, o con un nombre
# seguido de dos puntos.
RE_LINEA_DIALOGO = re.compile(
    r"^\s*(?:[-–—]\s*\S|[A-ZÁÉÍÓÚÑ][\wáéíóúñ]{1,18}\s*:\s+\S)"
)


def acotar_preambulo(caption, limite=LIMITE_PREAMBULO):
    """Corta la reseña a una línea cuando abajo hay diálogo.

    Esto es un freno de mano, no la regla: la regla está escrita en el prompt y
    casi siempre alcanza. Está acá porque el largo de la reseña es la cosa que
    más se desmadra sola —empieza en una línea y para el post veinte ya son
    cuatro— y una instrucción de texto no da garantía, un recorte en código sí.
    Cada post se pide en una llamada nueva y sin memoria de las anteriores, o
    sea que no hay forma de que la deriva se acumule, pero igual conviene tener
    el tope acá abajo, donde no depende de que nadie interprete nada.

    Solo actúa si hay diálogo debajo: sin diálogo la reseña es todo lo que hay y
    tiene que poder ser larga. Y nunca corta a mitad de oración: si la primera
    oración sola ya se pasa del límite, se respeta entera.
    """
    if not caption:
        return caption
    lineas = caption.split("\n")
    corte = next(
        (i for i, l in enumerate(lineas) if RE_LINEA_DIALOGO.match(l)), None
    )
    if corte is None or corte == 0:
        return caption          # no hay diálogo, o arranca en diálogo: no toco nada

    cabeza = "\n".join(lineas[:corte])
    preambulo = cabeza.strip()
    if len(preambulo) <= limite:
        return caption

    # Se van sumando oraciones enteras mientras entren; siempre queda una.
    partes = re.findall(r"[^.!?]+[.!?]*\s*", preambulo) or [preambulo]
    recorte = partes[0]
    for parte in partes[1:]:
        if len((recorte + parte).strip()) > limite:
            break
        recorte += parte
    recorte = recorte.strip()
    if recorte == preambulo:
        return caption

    log(
        f"Reseña recortada: {len(preambulo)} -> {len(recorte)} caracteres "
        f"(el tope con diálogo es {limite})."
    )
    return "\n".join([recorte] + lineas[corte:])


# ---------------------------------------------------------------------------
# Guion del reel
# ---------------------------------------------------------------------------

# Techo de la narración, en segundos. Esto NO es a lo que apunta el guion: es
# el corte de seguridad por si se desmadra. El orden de prioridades es al revés
# de lo que parece —primero que entren la reseña, el diálogo y el remate; el
# tiempo sale de ahí, no al revés—, así que este número solo tiene que ser lo
# bastante grande para que una historia larga quepa entera.
#
# Historia del número: 28 daban 70 palabras y cortaban el remate; 32 daban 80 y
# seguían apretando. Cincuenta dan 125 palabras, que a ritmo medido son unos 50
# segundos hablados, y el video frena en 52 (video.DURACION_MAX) para dejar aire
# después de la última palabra. Un guion normal sigue saliendo de 25 a 35
# segundos: el techo alto no alarga los videos, solo deja de mutilar los largos.
SEGUNDOS_DE_NARRACION = 50

# Lo mismo que usa el compositor de imágenes, para poder deshacer la censura de
# maquillaje en el texto hablado: leído en voz alta, "1d10t4" no suena a nada.
DESMAQUILLAJE = str.maketrans({"4": "a", "3": "e", "1": "i", "0": "o"})


def _limpiar_titulo_reel(texto, respaldo=""):
    """Deja el letrero de arriba listo para dibujar.

    Recorta a lo que entra en el banner sin partir palabras, saca hashtags y
    virgulillas, y lo pasa a mayúsculas. Los emojis se respetan: el banner los
    dibuja aparte con la fuente de color.
    """
    crudo = (texto or "").strip() or (respaldo or "").strip()
    crudo = re.sub(r"[#~]", "", crudo)
    crudo = re.sub(r"\s+", " ", crudo).strip()
    if not crudo:
        return ""
    if len(crudo) > 45:
        recorte = crudo[:45]
        if " " in recorte:
            recorte = recorte[: recorte.rfind(" ")]
        crudo = recorte.rstrip(" ,;:.-")
    return crudo.upper()


def _limpiar_narracion(texto, segundos=SEGUNDOS_DE_NARRACION):
    """Deja el guion hablado listo para mandárselo a la voz.

    Saca lo que no se puede leer en voz alta (hashtags, emojis, arrobas, los
    "Nombre:" de los diálogos) y lo recorta a las palabras que entran en el
    tiempo del reel, cortando siempre al final de una oración para que no quede
    la frase colgada.
    """
    crudo = (texto or "").strip()
    if not crudo:
        return ""
    crudo = video.RE_EMOJI.sub("", crudo)
    crudo = re.sub(r"[#@]\w+", "", crudo)
    crudo = crudo.replace("~", "")
    # Un "Fabio:" al principio de renglón es una acotación de guion, no habla.
    crudo = re.sub(r"(?m)^\s*[-–—]?\s*([A-ZÁÉÍÓÚÑ][\wáéíóúñ]{1,14}):\s*", r"\1 dijo: ", crudo)
    # Palabras maquilladas. No deberían llegar aquí —el guion hablado se pide
    # sin insultos, esquivándolos con otras palabras—, pero si se cuela una, se
    # borra: descifrarla haría que la voz diga el insulto en voz alta, que es
    # justo lo que la censura evita.
    crudo = re.sub(
        r"\b\w*[4310]\w*\b",
        lambda m: "" if re.search(r"[a-záéíóúñ]", m.group(0), re.I) else m.group(0),
        crudo,
    )
    crudo = re.sub(r"\s+([,.;:!?])", r"\1", crudo)
    crudo = re.sub(r"\s+", " ", crudo).strip(" -–—")

    tope = voz.cuanto_texto_entra(segundos)
    palabras = crudo.split()
    if len(palabras) <= tope:
        return crudo
    recorte = " ".join(palabras[:tope])
    corte = max(recorte.rfind(". "), recorte.rfind("! "), recorte.rfind("? "))
    if corte > len(recorte) * 0.5:
        return recorte[: corte + 1]
    return recorte.rstrip(" ,;:") + "."


def guion_de_reel(edit, texto_original=""):
    """Saca del resultado de Claude lo que necesita el reel.

    Devuelve {"titulo": ..., "narracion": ...} o None si no hay con qué narrar.
    El título tiene respaldo (la primera frase resaltada, o el arranque del post
    original), pero la narración no: sin guion no hay reel, y en ese caso el post
    sale como foto, que es lo que se venía haciendo.
    """
    lineas = [l.get("text", "") for l in (edit.get("lines") or [])]
    respaldo = re.sub(r"^[^:]{1,16}:\s*", "", lineas[0]) if lineas else texto_original
    titulo = _limpiar_titulo_reel(edit.get("titulo_reel"), respaldo)
    narracion = _limpiar_narracion(edit.get("narracion"))
    if len(narracion.split()) < 12:
        return None
    return {"titulo": titulo, "narracion": narracion}


def build_compose_spec(image_paths, edit, tmpdir):
    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    images_spec = []
    for idx, path in enumerate(image_paths):
        lines = [
            {"text": l["text"], "color": l.get("color", "white")}
            for l in edit.get("lines", [])
            if l.get("image_index") == idx
        ]
        images_spec.append({"path": str(path), "lines": lines, "pos": 0.84})
    spec = {"width": 1080, "line_size": 51, "images": images_spec}
    spec_path = tmpdir / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False))
    return spec_path


def armar_diapositivas(image_paths, edit, tmpdir):
    """Arma cada foto del post por separado, para el carrusel de Instagram.

    Es la misma imagen de siempre pero sin apilar: una foto por diapositiva,
    cada una con la frase que Claude le puso a ELLA. Las frases ya vienen
    separadas por foto en la respuesta (cada una trae su image_index), así que
    acá no se le pide nada nuevo a nadie: esto es dibujar píxeles, gratis.

    Devuelve la lista de imágenes armadas. Si algo falla, devuelve lo que haya:
    el carrusel se descarta solo si quedan menos de dos.
    """
    if len(image_paths) < 2:
        return []
    hechas = []
    for idx, path in enumerate(image_paths):
        # La frase de esta foto pasa a ser la de la foto 0 de su propia
        # diapositiva, porque ahí adentro es la única que hay.
        suyas = [dict(l, image_index=0) for l in edit.get("lines", [])
                 if l.get("image_index") == idx]
        try:
            spec = build_compose_spec([path], {"lines": suyas},
                                      tmpdir / f"dia{idx}")
            salida = tmpdir / f"diapositiva_{idx}.jpg"
            compose_image(spec, salida)
            hechas.append(salida)
        except Exception as e:
            log(f"No pude armar la diapositiva {idx} ({e}).")
    return hechas


def compose_image(spec_path, out_path):
    subprocess.run(
        [sys.executable, str(COMPOSE_SCRIPT), str(spec_path), str(out_path)],
        check=True,
    )


def avisar(texto):
    """Un mensaje suelto al chat de Telegram. Si no se puede, no pasa nada.

    Existe para las cosas que hay que contar en el momento y que si no se
    cuentan se pierden: sobre todo, que un post con la marca #UR terminó
    saliendo en foto porque algo falló armando el video. Sin esto, el bot hace
    lo correcto —publica la foto para no perder el post— pero parece que la
    regla del #UR no funcionara.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": texto[:4000]},
            timeout=30,
        )
        return r.status_code < 400
    except Exception as e:
        log(f"No se pudo avisar por Telegram ({e}).")
        return False


def send_telegram_preview(image_path, caption, details, post_id):
    """Manda la imagen editada + info del post a un chat de Telegram, para
    revisión manual cuando el bot está en modo seguro (DRY_RUN). No publica
    nada en Facebook; es solo una notificación con todo lo necesario para que
    una persona decida y postee a mano en donde corresponda."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram no configurado (faltan TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID); se omite envío.")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        with open(image_path, "rb") as f:
            r = requests.post(
                f"{base}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                files={"photo": f},
                timeout=30,
            )
        r.raise_for_status()
        r2 = requests.post(
            f"{base}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": details[:4096]},
            timeout=30,
        )
        r2.raise_for_status()
        log(f"Enviado a Telegram: post {post_id}")
        return True
    except Exception as e:
        log(f"Error enviando a Telegram: {e}")
        return False


def mandar_video_al_chat(reel_path, caption, enlace=None, log=log):
    """Manda al chat el MISMO video que se acaba de publicar, con su
    descripción en un mensaje aparte para copiar de un toque.

    No cuesta tokens: el archivo ya está renderizado (es el que salió en la
    página 2 y en Instagram); esto es solo un envío más por Telegram, para
    tenerlo a mano y poder subirlo a otro lado. Si algo falla acá, no pasa
    nada: la publicación ya salió, se anota en el log y se sigue.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    aviso = "🎬 Copia del video que acaba de salir."
    if enlace:
        aviso += f"\n{enlace}"
    try:
        with open(reel_path, "rb") as f:
            r = requests.post(f"{base}/sendVideo",
                              data={"chat_id": TELEGRAM_CHAT_ID,
                                    "caption": aviso[:1024],
                                    "supports_streaming": True},
                              files={"video": f}, timeout=180)
        r.raise_for_status()
    except Exception as e:
        log(f"No se pudo mandar la copia del video al chat: {e}")
        return False
    try:
        if caption:
            requests.post(f"{base}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID,
                                "text": caption[:4096]}, timeout=30)
    except Exception as e:
        log(f"La copia del video llegó, pero la descripción no: {e}")
    log("Copia del video mandada al chat.")
    return True


def consultar_sin_dialogo(image_path, texto_original, post_id, motivo, log=log):
    """Cuando un post normal no trae diálogo, en vez de descartarlo en
    silencio se te pregunta por Telegram qué hacer: sacarlo como foto, como
    video, o dejarlo fuera. Los botones encargan por el mismo camino que el
    panel de publicados (cola.pedir_video), así que al elegir sale en el
    próximo barrido con la frase gancho y la reseña armadas igual.

    El post queda marcado como procesado: la consulta se manda UNA sola vez.
    Devuelve True si el mensaje llegó al chat.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram no configurado; no se pudo consultar el post sin diálogo.")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    aviso = (f"🤔 SIN DIÁLOGO — Claude no encontró diálogo aprovechable, así "
             f"que este post NO salió solo. Motivo: {str(motivo or '')[:250]}\n\n"
             f"¿Lo saco igual?\n\n"
             f"— Texto original —\n{(texto_original or '')[:550]}")
    botones = {"inline_keyboard": [
        [{"text": "📷 Sacarlo como foto", "callback_data": f"s|f|{post_id}"}],
        [{"text": "🎬 Sacarlo como video", "callback_data": f"s|v|{post_id}"}],
        [{"text": "🗑 Dejarlo fuera", "callback_data": "s|x"}],
    ]}
    try:
        with open(image_path, "rb") as f:
            r = requests.post(f"{base}/sendPhoto",
                              data={"chat_id": TELEGRAM_CHAT_ID,
                                    "caption": aviso[:1024],
                                    "reply_markup": json.dumps(botones)},
                              files={"photo": f}, timeout=60)
        r.raise_for_status()
        log(f"Post {post_id}: sin diálogo; se preguntó por Telegram qué hacer.")
        return True
    except Exception as e:
        log(f"No se pudo consultar el post sin diálogo: {e}")
        return False


def mandar_aparte(image_path, caption, reel_path, post_id, video_caido=None,
                  texto_original="", log=log, nota=None):
    """Manda al chat lo que se preparó para un post apartado, y nada más.

    Va en tres mensajes a propósito. Primero la imagen con un aviso corto, para
    que se vea de un vistazo qué es y por qué no salió. Después la descripción
    SOLA, en un mensaje aparte y sin adornos: así se copia entera de un toque,
    sin arrastrar el aviso ni quedar cortada por el límite de 1024 caracteres
    que tienen los pies de foto. Y por último el video, si es que se armó.

    No publica nada, no toca el reloj de publicaciones y no llama a Instagram.
    Devuelve True si al menos la imagen llegó.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram no configurado; el post apartado no se pudo mandar.")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    motivo = (f"Este post lleva {ETIQUETA_APARTE}"
              if lleva_marca_aparte(texto_original)
              else "El bot está en modo solo-Telegram y hoy no publica nada en "
                   "automático")
    aviso = (f"📌 APARTADO — {motivo}, así que NO salió en la página 2 ni en "
             f"Instagram.\n\n"
             f"Te dejo la imagen ya armada y, en el mensaje de abajo, la "
             f"descripción lista para copiar.")
    if reel_path:
        aviso += "\n\nTambién te mando el video."
    elif video_caido:
        aviso += (f"\n\n⚠️ Llevaba {ETIQUETA_VIDEO} y el video no se pudo armar, "
                  f"así que va solo la foto. Motivo: {str(video_caido)[:300]}")
    if nota:
        aviso += "\n\n" + nota
    try:
        with open(image_path, "rb") as f:
            r = requests.post(f"{base}/sendPhoto",
                              data={"chat_id": TELEGRAM_CHAT_ID,
                                    "caption": aviso[:1024]},
                              files={"photo": f}, timeout=60)
        r.raise_for_status()
    except Exception as e:
        log(f"No se pudo mandar la imagen del post apartado: {e}")
        return False

    # De acá para abajo, si algo falla ya da igual: la imagen llegó y el post
    # queda marcado. Se avisa en el log y se sigue.
    try:
        r = requests.post(f"{base}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID,
                                "text": (caption or "")[:4096]}, timeout=60)
        r.raise_for_status()
    except Exception as e:
        log(f"No se pudo mandar la descripción del post apartado: {e}")

    if reel_path:
        try:
            with open(reel_path, "rb") as f:
                r = requests.post(f"{base}/sendVideo",
                                  data={"chat_id": TELEGRAM_CHAT_ID},
                                  files={"video": f}, timeout=300)
            r.raise_for_status()
        except Exception as e:
            log(f"No se pudo mandar el video del post apartado: {e}")

    log(f"Post {post_id}: apartado y mandado al chat; no se publicó en ningún lado.")
    return True


def publish_photo(image_path, caption):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID_BACKUP}/photos"
    with open(image_path, "rb") as f:
        files = {"source": f}
        data = {"caption": caption, "access_token": PAGE_TOKEN_BACKUP}
        r = requests.post(url, files=files, data=data, timeout=60)
    r.raise_for_status()
    return r.json()


def publish_reel(video_path, description):
    """Sube el reel a la página de respaldo, en los tres pasos que pide Facebook.

    1) Se avisa que empieza una subida y el servidor devuelve un video_id y una
       dirección donde dejar el archivo.
    2) Se manda el archivo entero a esa dirección.
    3) Se cierra la subida diciendo que se publique, con la descripción.

    Devuelve el id del reel publicado.
    """
    video_path = Path(video_path)
    peso = video_path.stat().st_size
    base = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID_BACKUP}/video_reels"

    inicio = requests.post(
        base,
        data={"upload_phase": "start", "access_token": PAGE_TOKEN_BACKUP},
        timeout=60,
    )
    inicio.raise_for_status()
    datos = inicio.json()
    video_id = datos.get("video_id")
    destino = datos.get("upload_url")
    if not video_id or not destino:
        raise RuntimeError(f"Facebook no dio dónde subir el reel: {datos}")

    with open(video_path, "rb") as f:
        subida = requests.post(
            destino,
            headers={
                "Authorization": f"OAuth {PAGE_TOKEN_BACKUP}",
                "offset": "0",
                "file_size": str(peso),
                "Content-Type": "application/octet-stream",
            },
            data=f,
            timeout=600,
        )
    subida.raise_for_status()
    if not (subida.json() or {}).get("success", True):
        raise RuntimeError(f"La subida del reel no terminó bien: {subida.text[:300]}")

    cierre = requests.post(
        base,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": description,
            "access_token": PAGE_TOKEN_BACKUP,
        },
        timeout=120,
    )
    cierre.raise_for_status()
    respuesta = cierre.json()
    if not respuesta.get("success", True):
        raise RuntimeError(f"Facebook rechazó publicar el reel: {respuesta}")
    log(f"Reel subido: video_id {video_id} ({peso / 1_000_000:.1f} MB)")
    return video_id


# ---------------------------------------------------------------------------
# Cuándo sale foto y cuándo sale reel
# ---------------------------------------------------------------------------

FORMATO_PATH = BASE_DIR / "state" / "formato.json"

# La marca que se pone a mano en la publicación de la página 1 para que esa —y
# solo esa— salga en video. Antes el formato se repartía por turnos (uno de cada
# tres), pero los videos rinden menos que las fotos, así que ahora el video no
# se sortea: se pide.
ETIQUETA_VIDEO = (os.environ.get("ETIQUETA_VIDEO") or "#UR").strip()


# La marca que APARTA el post: se prepara todo igual (imagen y descripción)
# pero no se publica en ningún lado; se manda al chat de Telegram y ahí termina.
# Sirve para material de otra página, que pasa por el mismo molde pero no va a
# la página 2. Como ETIQUETA_VIDEO, se puede cambiar desde el ci.yml sin tocar
# código, y dejándola vacía la regla se apaga entera.
ETIQUETA_APARTE = (os.environ.get("ETIQUETA_APARTE") or "#topchefvip5").strip()

# Cuántos apartados se preparan como máximo en un mismo barrido. No ocupan el
# cupo de publicación —no salen a ninguna página— pero cada uno cuesta una
# llamada a Claude y una imagen, así que conviene un techo para que un aluvión
# no deje una corrida dando vueltas diez minutos. Lo que sobra sale en el
# barrido siguiente, tres minutos después.
APARTADOS_POR_CORRIDA = env_num("APARTADOS_POR_CORRIDA", 3, int)


def _regex_etiqueta(marca=None):
    """La marca como palabra entera, para que #UR no se confunda con #URTV."""
    cuerpo = re.escape((marca if marca is not None else ETIQUETA_VIDEO).lstrip("#"))
    return re.compile(rf"#\s*{cuerpo}(?![\w])", re.IGNORECASE | re.UNICODE)


def pide_video(texto):
    """¿El original de la página 1 trae la marca que pide video?"""
    if not ETIQUETA_VIDEO:
        return False
    return bool(_regex_etiqueta(ETIQUETA_VIDEO).search(texto or ""))


def solo_telegram():
    """¿Está puesto el modo en que NADA se publica y todo va al chat?

    Es el freno de mano. Se enciende con la variable SOLO_TELEGRAM=1 y hace que
    todos los posts se comporten como si llevaran la marca de apartar: se
    preparan igual —imagen, descripción y video si corresponde— pero ninguno
    sale a Facebook ni a Instagram; llegan al chat y ahí quedan, para subirlos
    a mano.

    Se hizo para el día que la cuenta de Meta quedó en revisión: mientras eso
    dure, publicar automático no es una opción, pero el trabajo de preparar el
    material se puede seguir haciendo igual.
    """
    valor = (os.environ.get("SOLO_TELEGRAM") or "0").strip().lower()
    return valor in ("1", "si", "sí", "on", "true", "yes")


def lleva_marca_aparte(texto):
    """¿El texto trae la marca de apartar (hoy #topchefvip5)?"""
    if not ETIQUETA_APARTE:
        return False
    return bool(_regex_etiqueta(ETIQUETA_APARTE).search(texto or ""))


def va_aparte(texto):
    """¿Este post es para apartar en vez de publicarlo?

    Se prepara igual que cualquier otro —misma imagen, misma descripción— pero
    en vez de ir a la página 2 se manda al chat y ahí queda. No toca Facebook
    ni Instagram ni el reloj de publicaciones.

    Son dos motivos distintos y los dos valen: que el post traiga la marca, o
    que esté puesto el freno de mano que aparta TODO.
    """
    return solo_telegram() or lleva_marca_aparte(texto)


def programa_de(texto):
    """De qué reality es este post, si no es el de siempre.

    Va pegado a la MARCA, no a que el post se aparte. La diferencia importa
    desde que existe el freno de mano: con SOLO_TELEGRAM=1 se apartan todos los
    posts, y sería un disparate decirle a Claude que los de La Casa de los
    Famosos son de Top Chef solo porque no se están publicando.
    """
    return "topchef" if lleva_marca_aparte(texto) else None


def quitar_etiqueta(texto):
    """Saca la marca de video del texto: es una orden interna, no contenido.

    Se limpia antes de mandarle el texto a Claude y otra vez sobre lo que Claude
    devuelve, porque si #UR se colara en la descripción quedaría a la vista de
    todo el mundo un hashtag que no significa nada para quien lee.

    Ojo con lo que NO se borra: la marca de apartar (#topchefvip5) se deja tal
    cual. Esa sí es un hashtag de verdad, del programa, y sirve al post allá
    donde se vuelva a publicar. Antes se borraba por simetría con #UR y estaba
    mal: son dos cosas distintas. #UR es una orden para el bot y no significa
    nada para nadie más; #topchefvip5 es contenido.

    Efecto secundario bueno: como ahora Claude sí ve esa marca, le llega otra
    señal más de qué programa es, además del bloque de contexto.
    """
    limpio = texto or ""
    if ETIQUETA_VIDEO:
        limpio = _regex_etiqueta(ETIQUETA_VIDEO).sub("", limpio)
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    limpio = re.sub(r"\n{3,}", "\n\n", limpio)
    return limpio.strip()


def _anotar_formato(formato):
    """Suma uno a la cuenta, para que el reparto siga su curso."""
    try:
        estado = json.loads(FORMATO_PATH.read_text())
    except Exception:
        estado = {}
    estado["publicadas"] = int(estado.get("publicadas", 0)) + 1
    estado[formato] = int(estado.get(formato, 0)) + 1
    estado["ultimo"] = formato
    try:
        FORMATO_PATH.parent.mkdir(parents=True, exist_ok=True)
        FORMATO_PATH.write_text(json.dumps(estado, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"No se pudo anotar el formato ({e}); sigo igual.")


def elegir_formato(guion, cuantas_fotos, forzado=None, texto=""):
    """Devuelve "reel" o "foto", con el motivo, para dejarlo en el log.

    En automático el formato ya no se sortea: sale video solo si el original de
    la página 1 lleva la marca (por defecto #UR). Sin marca, foto. Lo que se
    manda a mano desde el bot no cambia: si el administrador pidió un formato,
    ese manda por encima de todo.

    Aun con la marca, el video necesita voz configurada y guion de Claude. Si
    falta alguno sale foto —que nunca falla— y el motivo queda escrito, para no
    quedarse con la duda de por qué el #UR no dio video.
    """
    if forzado in ("foto", "reel"):
        if forzado == "reel" and not (guion and voz.hay_voz()):
            return "foto", "se pidió video pero no hay voz o guion"
        return forzado, "lo pidió el administrador"
    if not cuantas_fotos:
        return "foto", "no hay fotos"
    if not pide_video(texto):
        return "foto", f"el original no lleva {ETIQUETA_VIDEO}"
    if not voz.hay_voz():
        return "foto", f"lleva {ETIQUETA_VIDEO} pero falta la voz (VOZ_API_KEY)"
    if not guion:
        return "foto", f"lleva {ETIQUETA_VIDEO} pero Claude no dejó guion narrado"
    return "reel", f"el original lleva {ETIQUETA_VIDEO}"


def armar_reel(local_images, guion, tmpdir):
    """Genera el mp4 del reel: voz, subtítulos y video. Devuelve la ruta."""
    audio = tmpdir / "voz.mp3"
    ficha = voz.sintetizar(guion["narracion"], audio)

    subs = tmpdir / "subs.ass"
    subtitulos.escribir_ass(
        guion["narracion"], ficha["segundos"], subs,
        marcas=ficha.get("marcas"),
        margen_abajo=video.margen_subtitulos(False),
    )

    salida = tmpdir / "reel.mp4"
    video.armar(
        salida,
        fotos=[str(p) for p in local_images],
        titulo=guion["titulo"],
        audio=str(audio),
        subtitulos=str(subs),
        tmpdir=str(tmpdir / "trabajo"),
    )
    return salida


def process_post(post, tmpdir, allow_publish=True):
    post_id = post["id"]
    kind, images = classify_attachment(post)

    if kind == "video":
        log(f"Post {post_id}: es video/reel, se omite.")
        return "skipped_video"
    if kind != "photo" or not images:
        log(f"Post {post_id}: sin fotos aprovechables, se omite.")
        return "skipped_no_photo"

    text = post.get("message", "").strip()
    if not text:
        log(f"Post {post_id}: sin texto, se omite.")
        return "skipped_no_text"

    # Este post SÍ es publicable. Si todavía no toca (ya se publicó algo hace
    # poco, o ya salió uno en esta corrida), se deja pendiente tal cual: no se
    # marca como procesado ni se gasta una llamada a Claude. Sale en la
    # siguiente corrida que le toque.
    # Los apartados no esperan turno. El espaciado existe para que la página 2
    # no reciba varios posts de golpe, y estos no van a la página 2: van a tu
    # chat, para que los repostees a mano en otro lado. Hacerlos esperar 10
    # minutos era freno sin motivo.
    if not allow_publish and not DRY_RUN and not va_aparte(text):
        log(f"Post {post_id}: publicable, pero toca esperar el turno; queda pendiente.")
        return "deferred"

    local_images = []
    for i, url in enumerate(images):
        dest = tmpdir / f"{post_id}_{i}.jpg"
        download_image(url, dest)
        local_images.append(dest)

    # El formato se sabe ANTES de llamar a Claude, así que el guion narrado se
    # pide solo cuando de verdad va a haber video. Ojo con el orden: acá todavía
    # no se decide el formato definitivo (eso lo hace elegir_formato más abajo,
    # que además mira si hay voz), esto es solo si vale la pena pedir el guion.
    pedido = cola.formato_pedido(post_id)
    con_video = pedido == "reel" or (pedido != "foto" and pide_video(text))

    # A Claude se le manda el texto SIN la marca: es una orden para el bot, no
    # contenido, y si la viera podría terminar copiándola en la descripción.
    texto_limpio = quitar_etiqueta(text)
    # De qué programa es. Hay que decírselo porque la marca se borra arriba: sin
    # esto Claude lee el diálogo suelto y lo escribe con el molde del reality de
    # siempre, que no es el mismo mundo ni el mismo vocabulario.
    programa = programa_de(text)
    # Los apartados van al chat para subida manual: ahí decide el administrador,
    # no el filtro editorial, así que a Claude se le prohíbe omitirlos.
    es_aparte = va_aparte(text)
    edit = ask_claude(texto_limpio, len(local_images), con_video=con_video,
                      programa=programa, aparte=es_aparte)
    if edit.get("skip"):
        if es_aparte:
            # Red de seguridad: no debería pasar con el pedido reforzado, pero
            # si igual lo omite, al chat va la foto original con el texto tal
            # cual. Peor sería que el apartado se pierda en silencio.
            log(f"Post {post_id}: Claude quiso omitirlo "
                f"({edit.get('skip_reason')}), pero es apartado: va igual al "
                f"chat con la foto y el texto originales.")
            mandar_aparte(local_images[0], quitar_etiqueta(text), None, post_id,
                          texto_original=text, log=log,
                          nota=("⚠️ Claude no encontró diálogo aprovechable, "
                                "así que va la foto original sin editar y el "
                                "texto tal cual."))
            return "apartado"
        # Post normal sin diálogo: ya no se descarta en silencio. Se te
        # pregunta por Telegram si lo querés como foto, como video o fuera.
        log(f"Post {post_id}: Claude decidió omitir ({edit.get('skip_reason')}); "
            f"se consulta por Telegram.")
        consultar_sin_dialogo(local_images[0], text, post_id,
                              edit.get("skip_reason"), log=log)
        return "skipped_by_ai"

    spec_path = build_compose_spec(local_images, edit, tmpdir)
    out_path = tmpdir / f"{post_id}_out.jpg"
    compose_image(spec_path, out_path)

    caption = quitar_etiqueta(acotar_preambulo(edit.get("caption", "").strip()))
    if not caption:
        caption = (PROGRAMAS.get(programa) or {}).get("hashtag", "#LCDLF6")

    # El mismo post puede salir como foto o como reel. La foto ya está armada
    # arriba y sirve igual de vista previa, así que el video se arma solo si le
    # toca; si algo falla armándolo, se publica la foto y no se pierde el post.
    guion = guion_de_reel(edit, texto_limpio) if con_video else None
    formato, motivo = elegir_formato(
        guion, len(local_images), pedido, texto=text
    )
    log(f"Post {post_id}: sale como {formato} ({motivo}).")
    reel_path = None
    # Si pidió video y termina en foto, hay que decirlo. Se guarda el porqué acá
    # y se avisa recién cuando el post ya salió, para no cantar victoria antes.
    video_caido = None
    if formato == "reel":
        try:
            reel_path = armar_reel(local_images, guion, tmpdir)
        except Exception as e:
            log(f"No se pudo armar el reel ({e}); sale como foto.")
            video_caido = str(e)
            formato, reel_path = "foto", None

    if DRY_RUN:
        preview_dir = BASE_DIR / "dry_run_output"
        preview_dir.mkdir(exist_ok=True)
        stub = post_id.split("_")[-1]
        preview_img = preview_dir / f"{stub}.jpg"
        shutil.copy(out_path, preview_img)
        details = (
            f"POST ID: {post_id}\n\n--- TEXTO ORIGINAL ---\n{text}\n\n"
            f"--- FRASES ELEGIDAS ---\n"
            + "\n".join(f"[img {l.get('image_index')}] ({l.get('color')}) {l.get('text')}"
                        for l in edit.get("lines", []))
            + f"\n\n--- DESCRIPCION ALTERNA ---\n{caption}\n"
            + f"\n--- FORMATO ---\n{formato} ({motivo})\n"
            + (f"\n--- TITULO DEL VIDEO ---\n{guion['titulo']}\n"
               f"\n--- NARRACION ---\n{guion['narracion']}\n" if guion else "")
        )
        (preview_dir / f"{stub}.txt").write_text(details, encoding="utf-8")
        if reel_path:
            shutil.copy(reel_path, preview_dir / f"{stub}.mp4")
        log(f"[DRY_RUN] Preview guardado: {preview_img.name}. Caption: {caption}")
        send_telegram_preview(out_path, caption, details, post_id)
        return "dry_run"

    # Apartado: acá se corta. Todo lo de arriba ya se hizo (la imagen está
    # armada, la descripción escrita y, si llevaba la marca de video, el reel
    # también), pero de acá no pasa a Facebook. Va al chat y el post queda
    # marcado para que no vuelva a aparecer.
    if va_aparte(text):
        mandar_aparte(out_path, caption, reel_path, post_id,
                      video_caido=video_caido, texto_original=text, log=log)
        return "apartado"

    if formato == "reel" and reel_path:
        try:
            backup_post_id = publish_reel(reel_path, caption)
        except Exception as e:
            log(f"Falló la publicación del reel ({e}); lo publico como foto.")
            video_caido = str(e)
            formato = "foto"
            backup_post_id = None
        if backup_post_id:
            record_published(backup_post_id, post_id, text, caption, "reel")
            mark_published_now("auto")
            _anotar_formato("reel")
            # Solo cuando el video salió de verdad: así el próximo abre distinto.
            _anotar_arranque(guion.get("narracion") if guion else "")
            log(f"Post {post_id} -> publicado como reel {backup_post_id}")
            # El mismo archivo, ya renderizado, también a Instagram. Va al final
            # y envuelto: si falla, el reel de Facebook ya salió y quedó anotado.
            try:
                insta.publicar_reel(PAGE_ID_BACKUP, PAGE_TOKEN_BACKUP,
                                    backup_post_id, caption, reel_path, log=log)
            except Exception as e:
                log(f"Instagram quedó afuera esta vez ({e}); el reel ya salió.")
            # El mismo archivo, de regalo al chat: cero tokens, solo un envío.
            mandar_video_al_chat(reel_path, caption,
                                 f"https://www.facebook.com/{backup_post_id}",
                                 log=log)
            return "published"

    result = publish_photo(out_path, caption)
    backup_post_id = result.get("post_id") or result.get("id")
    record_published(backup_post_id, post_id, text, caption, "foto")
    mark_published_now("auto")
    _anotar_formato("foto")
    log(f"Post {post_id} -> publicado como {backup_post_id}")
    # La misma foto y la misma descripción, ya hechas, van también a Instagram.
    # Va DESPUÉS de todo lo de Facebook y no devuelve nada que se use: si falla,
    # el post de la página 2 ya salió y quedó anotado igual. Las diapositivas
    # solo se arman si la apilada no entra, así no se gasta trabajo al pedo.
    try:
        sueltas = []
        if not insta.forma(out_path, log=lambda *a: None)[0]:
            sueltas = armar_diapositivas(local_images, edit, tmpdir)
        insta.publicar_foto(PAGE_ID_BACKUP, PAGE_TOKEN_BACKUP, result, caption,
                            ruta=out_path, diapositivas=sueltas, log=log)
    except Exception as e:
        log(f"Instagram quedó afuera esta vez ({e}); el post ya salió igual.")

    # Recién ahora, con el post ya publicado: si pedía video y salió foto, se
    # avisa. Es lo único que el bot hacía bien y no contaba, y por eso parecía
    # que la marca #UR no funcionaba.
    if video_caido:
        avisar(
            f"⚠️ Este post llevaba {ETIQUETA_VIDEO} y tenía que salir en video, "
            f"pero el video no se pudo armar y salió como foto para no perderlo.\n\n"
            f"Motivo: {video_caido[:400]}\n\n"
            f"La foto ya está publicada. Si querés el video igual, entrá a "
            f"📚 Publicados y pedíselo desde ahí."
        )
    return "published"


# ---------------------------------------------------------------------------
# Videos encargados sobre publicaciones que ya salieron
# ---------------------------------------------------------------------------

# Cuántos encargos se atienden por corrida. Uno: armar un reel se lleva un par
# de minutos y una llamada a Claude, y si se pidieron tres de golpe es mejor
# que salgan de a uno y espaciados que tres videos seguidos en la página 2.
MAX_REHACER_POR_CICLO = 1


def traer_post(post_id):
    """Vuelve a pedirle a Facebook un post de la página 1 por su identificador.

    Hace falta porque el encargo puede llegar días después: para entonces el
    post ya no viene en el barrido normal, que solo mira los recientes.
    """
    campos = ("id,message,created_time,attachments{media_type,type,url,media,"
              "subattachments{media,type,url}}")
    return graph_get(post_id, PAGE_TOKEN_MAIN, fields=campos)


def rehacer_como_video(pedido, tmpdir):
    """Arma y publica el video de un post que ya había salido como foto.

    Se rehace desde el ORIGINAL de la página 1 —misma foto, mismo texto—, no
    desde lo que se publicó, así el video sale igual que si le hubiera tocado
    video desde el principio. Lo que ya está publicado en la página 2 no se
    toca: si querés que quede solo el video, la foto la borrás vos.

    Devuelve (ok, detalle). El detalle es lo que se le muestra en el chat.
    """
    pid = str(pedido.get("source") or "")
    if not pid:
        return False, "el encargo no traía identificador"

    post = traer_post(pid)
    kind, images = classify_attachment(post)
    if kind != "photo" or not images:
        return False, "el post original ya no tiene fotos que se puedan usar"
    text = (post.get("message") or "").strip()
    if not text:
        return False, "el post original no tiene texto para narrar"

    local_images = []
    for i, url in enumerate(images):
        dest = tmpdir / f"rehacer_{i}.jpg"
        download_image(url, dest)
        local_images.append(dest)

    # Acá el video no se discute: es un encargo tuyo, así que se pide el guion
    # con manual=True (Claude no puede negarse aunque no haya diálogo) y con el
    # programa correcto para que el mood sea el del reality que toca.
    texto_limpio = quitar_etiqueta(text)
    programa = programa_de(text)
    edit = ask_claude(texto_limpio, len(local_images), con_video=True,
                      programa=programa, manual=True)
    if edit.get("skip"):
        return False, f"Claude prefirió no tocarlo ({edit.get('skip_reason')})"

    guion = guion_de_reel(edit, texto_limpio)
    if not guion:
        return False, "no salió guion para narrar, así que no hay video"

    caption = quitar_etiqueta(
        acotar_preambulo((edit.get("caption") or "").strip())) or "#LCDLF6"
    reel_path = armar_reel(local_images, guion, tmpdir)

    if DRY_RUN:
        log(f"[DRY_RUN] Reel encargado listo para {pid}, no se publica.")
        return True, "listo (modo de prueba: no se publicó)"

    destino = pedido.get("destino") or "ambos"

    nuevo = None
    if destino != "ig":
        nuevo = publish_reel(reel_path, caption)
        if not nuevo:
            return False, "Facebook no devolvió el identificador del video"
        record_published(nuevo, pid, text, caption, "reel")
        mark_published_now("rehacer")
        # Se cuenta igual, aunque lo hayas pedido a mano: la cuenta ya no decide
        # nada (el formato lo decide la marca #UR), pero sirve para saber de un
        # vistazo cuántos videos y cuántas fotos llevan.
        _anotar_formato("reel")
        _anotar_arranque(guion.get("narracion"))
        log(f"Encargo: {pid} -> publicado como reel {nuevo}")

    ig_post = None
    if destino != "fb":
        # A Instagram el video va como archivo (subida directa), así que no
        # necesita que exista el reel de Facebook: con destino "ig" se manda
        # igual, solo que sin identificador de respaldo.
        try:
            ig_post = insta.publicar_reel(PAGE_ID_BACKUP, PAGE_TOKEN_BACKUP,
                                          nuevo, caption, reel_path, log=log)
        except Exception as e:
            log(f"Instagram: no salió el video encargado ({e}).")
        if destino == "ig" and not ig_post:
            return False, "Instagram no aceptó el video; no se publicó en ningún lado"

    # También acá va la copia al chat: es el mismo archivo ya renderizado.
    mandar_video_al_chat(reel_path, caption,
                         f"https://www.facebook.com/{nuevo}" if nuevo else None,
                         log=log)

    if nuevo:
        return True, f"https://www.facebook.com/{nuevo}"
    return True, f"salió solo en Instagram ({ig_post})"


def rehacer_como_foto(pedido, tmpdir):
    """Vuelve a publicar como foto un post, releyendo el original de la página 1.

    Es el mismo camino que la publicación automática de fotos, solo que a
    pedido: se descargan las fotos y el texto del original TAL COMO ESTÉN AHORA
    —o sea que una corrección hecha en la página 1 entra acá—, Claude escribe la
    descripción de nuevo, se arma la imagen y sale a la página 2 y a Instagram.

    Existe para el día que una publicación salió mal o quedó invisible (como
    cuando la app estuvo en modo desarrollo) y la borraste: sin esto el panel
    solo sabía rehacer EN VIDEO, y rehacer una foto obligaba a mandarla a mano.
    """
    pid = str(pedido.get("source") or "")
    if not pid:
        return False, "el encargo no traía identificador"

    post = traer_post(pid)
    kind, images = classify_attachment(post)
    if kind != "photo" or not images:
        return False, "el post original ya no tiene fotos que se puedan usar"
    text = (post.get("message") or "").strip()
    if not text:
        return False, "el post original no tiene texto"

    local_images = []
    for i, url in enumerate(images):
        dest = tmpdir / f"refoto_{i}.jpg"
        download_image(url, dest)
        local_images.append(dest)

    texto_limpio = quitar_etiqueta(text)
    programa = programa_de(text)
    # manual=True: es un encargo tuyo, así que Claude no puede negarse aunque
    # el post no traiga diálogo; arma frase gancho y reseña igual.
    edit = ask_claude(texto_limpio, len(local_images), con_video=False,
                      programa=programa, manual=True)
    if edit.get("skip"):
        return False, f"Claude prefirió no tocarlo ({edit.get('skip_reason')})"

    spec_path = build_compose_spec(local_images, edit, tmpdir)
    out_path = tmpdir / "refoto_out.jpg"
    compose_image(spec_path, out_path)
    caption = quitar_etiqueta(acotar_preambulo((edit.get("caption") or "").strip()))
    if not caption:
        caption = (PROGRAMAS.get(programa) or {}).get("hashtag", "#LCDLF6")

    if DRY_RUN:
        log(f"[DRY_RUN] Foto encargada lista para {pid}, no se publica.")
        return True, "listo (modo de prueba: no se publicó)"

    destino = pedido.get("destino") or "ambos"

    result = None
    backup_post_id = None
    if destino != "ig":
        result = publish_photo(out_path, caption)
        backup_post_id = result.get("post_id") or result.get("id")
        record_published(backup_post_id, pid, text, caption, "foto")
        mark_published_now("rehacer")
        _anotar_formato("foto")
        log(f"Encargo: {pid} -> republicado como foto {backup_post_id}")

    ig_post = None
    if destino != "fb":
        try:
            sueltas = []
            if not insta.forma(out_path, log=lambda *a: None)[0]:
                sueltas = armar_diapositivas(local_images, edit, tmpdir)
            # Sin post de Facebook (solo-Instagram), publicar_foto sube una
            # copia oculta desde el archivo y la borra al terminar.
            ig_post = insta.publicar_foto(PAGE_ID_BACKUP, PAGE_TOKEN_BACKUP,
                                          result, caption, ruta=out_path,
                                          diapositivas=sueltas, log=log)
        except Exception as e:
            log(f"Instagram quedó afuera esta vez ({e}).")
        if destino == "ig" and not ig_post:
            return False, "Instagram no aceptó la foto; no se publicó en ningún lado"

    if backup_post_id:
        return True, f"https://www.facebook.com/{backup_post_id}"
    return True, f"salió solo en Instagram ({ig_post})"


def atender_encargos(limite=MAX_REHACER_POR_CICLO):
    """Atiende los encargos del chat (videos o fotos). Devuelve cuántos publicó."""
    pendientes = cola.rehacer_pendientes()
    if not pendientes:
        return 0
    log(f"Hay {len(pendientes)} encargo(s) desde el chat.")
    hechos = 0
    with tempfile.TemporaryDirectory() as tmp:
        for pedido in pendientes[:limite]:
            pid = pedido.get("source")
            try:
                if pedido.get("formato") == "foto":
                    ok, detalle = rehacer_como_foto(pedido, Path(tmp))
                elif pedido.get("formato") == "chat":
                    ok, detalle = reenviar_video_al_chat(pedido, Path(tmp))
                else:
                    ok, detalle = rehacer_como_video(pedido, Path(tmp))
            except Exception as e:
                ok, detalle = False, str(e)[:200]
            if ok:
                cola.cerrar_video(pid, "hecho", detalle, detalle)
                hechos += 1
            else:
                # Se cierra igual en vez de reintentar para siempre: si el
                # motivo es que el post ya no sirve, reintentar no lo arregla
                # y cada intento cuesta una llamada a Claude.
                cola.cerrar_video(pid, "error", detalle)
                log(f"Encargo {pid}: no se pudo ({detalle}).")
    return hechos


def reenviar_video_al_chat(pedido, tmpdir):
    """Reenvía al chat el video YA PUBLICADO de la página 2, tal cual salió.

    No regenera nada ni llama a Claude: le pide a Facebook el archivo del
    video con la llave de la página, lo baja y lo manda por Telegram junto
    con la descripción que tiene guardada. Sirve para recuperar videos que
    salieron antes de que existiera la copia automática al chat, o para
    volver a pedir uno viejo.
    """
    vid = str(pedido.get("publicado") or "")
    if not vid:
        return False, "no tengo anotado el identificador del video publicado"
    # El nodo de video en Graph es el número final (a veces viene PAGINA_VIDEO).
    video_id = vid.split("_")[-1]
    try:
        data = graph_get(video_id, PAGE_TOKEN_BACKUP, fields="source")
    except Exception as e:
        return False, f"Facebook no dio el video ({str(e)[:150]})"
    src = data.get("source")
    if not src:
        return False, "Facebook no entregó el archivo del video (¿lo borraste?)"
    ruta = tmpdir / "reenviar.mp4"
    r = requests.get(src, timeout=180)
    r.raise_for_status()
    ruta.write_bytes(r.content)
    # La descripción real quedó guardada cuando se publicó.
    caption = pedido.get("texto") or ""
    try:
        if PUBLISHED_MAP_PATH.exists():
            info = json.loads(PUBLISHED_MAP_PATH.read_text()).get(vid) or {}
            caption = info.get("caption") or caption
    except Exception:
        pass
    if mandar_video_al_chat(ruta, caption, f"https://www.facebook.com/{vid}",
                            log=log):
        return True, "el video ya está en el chat"
    return False, "Telegram no aceptó el video"


def record_published(backup_post_id, source_post_id, source_text, caption,
                     formato="foto"):
    """Guarda el mapeo post-de-CAM1 -> post-original, para poder regenerar luego."""
    try:
        data = {}
        if PUBLISHED_MAP_PATH.exists():
            data = json.loads(PUBLISHED_MAP_PATH.read_text())
        data[str(backup_post_id)] = {
            "source_post_id": source_post_id,
            "source_text": source_text,
            "caption": caption,
            # Cómo salió. Sirve para que el panel sepa a cuáles ofrecerles el
            # botón de "hacé el video": a los que ya salieron como video, no.
            # Los registros viejos no lo tienen y se toman como desconocidos.
            "formato": formato,
            "ts": os.environ.get("GITHUB_RUN_ID", ""),
            # Hora real de publicación, para poder decir "hace cuánto" en el chat.
            "when": time.time(),
        }
        # conserva solo los últimos 200
        if len(data) > 200:
            for k in list(data.keys())[:-200]:
                data.pop(k, None)
        PUBLISHED_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        PUBLISHED_MAP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"No se pudo registrar published_map: {e}")


def run_test_mode():
    """Toma la primera foto real de la página, le inyecta un diálogo de ejemplo
    (con términos fuertes para ver la censura) y genera la imagen editada, sin
    publicar. Ignora el estado. Solo para aprobar el estilo antes de lanzar."""
    posts = fetch_recent_posts()
    target = None
    images = None
    for p in posts:
        kind, imgs = classify_attachment(p)
        if kind == "photo" and imgs:
            target, images = p, imgs
            break
    if not target:
        log("TEST_MODE: no se encontró ningún post con foto en los recientes.")
        return
    sample = os.environ.get("TEST_TEXT") or (
        "Josh: Perdón por lo de Stefano y los comentarios que hice.\n"
        "Fabio: Es un juego, yo no me tomo las cosas personales.\n"
        "Guana: No seas idiota, eso que dijiste es puro racismo."
    )
    target = dict(target)
    target["message"] = sample
    log(f"TEST_MODE: usando la foto del post {target['id']} con diálogo de ejemplo.")
    with tempfile.TemporaryDirectory() as tmp:
        status = process_post(target, Path(tmp))
    log(f"TEST_MODE: resultado = {status}")


def main():
    if os.environ.get("BOT_ENABLED", "true").lower() == "false":
        log("BOT_ENABLED=false: el bot está apagado, no se hace nada.")
        return

    if os.environ.get("TEST_MODE", "false").lower() == "true":
        run_test_mode()
        return

    state = load_state()
    processed = set(state.get("processed", []))

    posts = fetch_recent_posts(processed)

    if os.environ.get("DEBUG", "false").lower() == "true":
        log(f"DEBUG: {len(posts)} posts recibidos de la API.")
        for p in posts:
            log("DEBUG raw post ->\n" + json.dumps(p, ensure_ascii=False, indent=2))
    pendientes = [p for p in posts if p["id"] not in processed]
    pendientes.sort(key=lambda p: p.get("created_time", ""))  # más viejo primero

    # ----------------------------------------------------------------------
    # Lo que se decidió desde el panel de Telegram
    # ----------------------------------------------------------------------
    control = cola.leer_control()
    eliminados = set(control.get("eliminados") or [])
    pausados = set(control.get("pausados") or [])
    prioridad = set(control.get("prioridad") or [])

    # Eliminados: se marcan como procesados para que no vuelvan a aparecer
    # nunca más. El post sigue intacto en la página 1; lo único que pasa es
    # que este bot ya no lo va a republicar.
    descartados = [p for p in pendientes if p["id"] in eliminados]
    if descartados:
        for p in descartados:
            processed.add(p["id"])
        state["processed"] = sorted(processed)
        save_state(state)
        cola.soltar_eliminados(p["id"] for p in descartados)
        pendientes = [p for p in pendientes if p["id"] not in eliminados]
        log(f"{len(descartados)} post(s) descartados desde el panel; no se publican.")

    # Foto de la cola para el panel. Se guarda ANTES de recortar por
    # MAX_POSTS_PER_RUN: el panel tiene que mostrar todo lo que espera, no
    # solo el que sale en esta corrida.
    try:
        cola.guardar_snapshot(
            [resumen_para_cola(p) for p in pendientes],
            extra={
                "min_entre_posts": MIN_MINUTES_BETWEEN_POSTS,
                "minutos_desde_ultima": minutes_since_last_publish(),
                "total_pendientes": len(pendientes),
            },
        )
    except Exception as e:
        log(f"No se pudo guardar la foto de la cola: {e}")

    # El control se poda con lo que sigue pendiente, para que no se acumulen
    # identificadores de posts que ya salieron.
    cola.limpiar_control(p["id"] for p in pendientes)

    # Los videos encargados a mano van antes que la cola: los pediste vos, no
    # tienen que esperar turno. Va acá y no más arriba a propósito, para que la
    # foto de la cola quede guardada aunque el encargo falle. Si se publica uno,
    # el reloj de publicación se mueve y el post de la cola se difiere solo
    # hasta la próxima corrida: no salen dos seguidos.
    try:
        atender_encargos()
    except Exception as e:
        log(f"No se pudieron atender los videos encargados: {e}")

    # Pausados: siguen en la cola y a la vista, pero no ocupan turno.
    congelados = [p for p in pendientes if p["id"] in pausados]
    if congelados:
        log(f"{len(congelados)} post(s) en pausa desde el panel; se saltan.")
    candidatos = [p for p in pendientes if p["id"] not in pausados]

    # "Publicar ahora": lo que marcaste se pone al frente de la fila.
    candidatos.sort(key=lambda p: (0 if p["id"] in prioridad else 1,
                                   p.get("created_time", "")))

    # Los apartados van primero y aparte del cupo. El cupo por corrida existe
    # para no inundar la página 2, y estos no la tocan: se preparan y se van a
    # tu chat. Si llegan cinco de golpe, salen los cinco en el mismo barrido en
    # vez de gotear de a uno. El tope de APARTADOS_POR_CORRIDA está para que un
    # aluvión no haga eterna una sola vuelta; lo que sobre sale en la siguiente,
    # tres minutos después.
    ids_aparte = {p["id"] for p in candidatos if va_aparte(p.get("message") or "")}
    apartados = [p for p in candidatos if p["id"] in ids_aparte][:APARTADOS_POR_CORRIDA]
    normales = [p for p in candidatos if p["id"] not in ids_aparte]
    if apartados:
        log(f"{len(apartados)} apartado(s) van sin esperar turno.")
    new_posts = apartados + normales[:MAX_POSTS_PER_RUN]

    if not new_posts:
        log("Sin posts nuevos." if not pendientes else
            f"Los {len(pendientes)} pendiente(s) están todos en pausa.")
        return

    # Turno de publicación: como máximo UNO por corrida y respetando el tiempo
    # mínimo desde la publicación anterior. Los posts que no son publicables
    # (video, sin foto, sin texto, descartados por Claude) sí se siguen
    # revisando y marcando, porque no ocupan turno.
    urgente = new_posts[0]["id"] in prioridad
    allow_publish = urgente or can_publish_now()
    mins = minutes_since_last_publish()
    if urgente:
        log("Pediste que este saliera ya desde el panel: se salta la espera y "
            "va primero.")
    elif allow_publish:
        log(f"Turno libre para publicar (última publicación hace "
            f"{'nunca' if mins is None else f'{mins:.1f} min'}).")
    else:
        log(f"En espera: la última publicación fue hace {mins:.1f} min y el mínimo "
            f"es {MIN_MINUTES_BETWEEN_POSTS:.0f} min. Los pendientes salen de a uno.")

    pendientes_restantes = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for post in new_posts:
            try:
                status = process_post(post, tmpdir, allow_publish=allow_publish)
            except Exception as e:
                log(f"ERROR procesando {post['id']}: {e}")
                continue

            if status == "deferred":
                # No se toca el estado: este post sigue pendiente para la
                # próxima corrida. Y como ya se sabe que hay cola, no tiene
                # sentido seguir revisando los de más atrás en esta corrida.
                pendientes_restantes = len(new_posts) - new_posts.index(post)
                break

            if status != "dry_run":
                processed.add(post["id"])
                state["processed"] = sorted(processed)
                save_state(state)

            if status == "published":
                # Ya salió el de esta corrida: el resto espera su turno.
                allow_publish = False
            time.sleep(2)

    # La foto de la cola se guardó ARRIBA, antes de publicar. Si en esta corrida
    # salió algo, esa foto quedó vieja al instante y el panel seguiría mostrando
    # el post con sus botones aunque ya no esté: uno le toca 🎬 y la orden se
    # guarda para un post por el que el bot ya no va a volver a pasar. Así que se
    # vuelve a guardar acá, ya sin lo que salió. No cuesta nada: es la misma
    # lista que ya teníamos en memoria, no se le pide nada a Facebook.
    quedan = [p for p in pendientes if p["id"] not in processed]
    if len(quedan) != len(pendientes):
        try:
            cola.guardar_snapshot(
                [resumen_para_cola(p) for p in quedan],
                extra={
                    "min_entre_posts": MIN_MINUTES_BETWEEN_POSTS,
                    "minutos_desde_ultima": minutes_since_last_publish(),
                    "total_pendientes": len(quedan),
                },
            )
            cola.limpiar_control(p["id"] for p in quedan)
        except Exception as e:
            log(f"No se pudo actualizar la foto de la cola: {e}")

    if pendientes_restantes:
        log(f"Quedan {pendientes_restantes} post(s) en cola; se publicarán de a uno "
            f"cada {MIN_MINUTES_BETWEEN_POSTS:.0f} min.")


if __name__ == "__main__":
    main()
