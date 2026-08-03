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
import sys
import json
import time
import shutil
import tempfile
import subprocess
from pathlib import Path

import requests

import cola

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

CLAUDE_SYSTEM_PROMPT = f"""Eres el editor de la página alterna "{NOMBRE_ALTERNA}", que reposta \
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

   a) PREÁMBULO: 1-3 oraciones que cuenten la situación con palabras DISTINTAS al post original \
      (tono enganchador), para que Facebook no lo marque como contenido duplicado.

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

Responde ÚNICAMENTE llamando a la herramienta submit_edit con el JSON estructurado."""

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
                    "Descripción alterna completa: preámbulo reescrito, luego el diálogo "
                    "del original (una línea por intervención, con guion y nombre, "
                    "separadas por saltos de línea reales) si lo hubiera, y al final "
                    "los hashtags."
                ),
            },
        },
        "required": ["skip"],
    },
}


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
    "Siempre devuelve skip=false con lines y caption completos."
)


def ask_claude(original_text, num_images, manual=False):
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_msg = (
        f"Texto original del post:\n---\n{original_text}\n---\n"
        f"Cantidad de fotos disponibles: {num_images} (índices 0"
        + (f" a {num_images - 1}" if num_images > 1 else "") + ")."
    )
    if manual:
        user_msg += MANUAL_OVERRIDE
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=CLAUDE_SYSTEM_PROMPT,
        tools=[SUBMIT_TOOL],
        tool_choice={"type": "tool", "name": "submit_edit"},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_edit":
            return block.input
    raise RuntimeError("Claude no devolvió submit_edit")


def build_compose_spec(image_paths, edit, tmpdir):
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


def compose_image(spec_path, out_path):
    subprocess.run(
        [sys.executable, str(COMPOSE_SCRIPT), str(spec_path), str(out_path)],
        check=True,
    )


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


def publish_photo(image_path, caption):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID_BACKUP}/photos"
    with open(image_path, "rb") as f:
        files = {"source": f}
        data = {"caption": caption, "access_token": PAGE_TOKEN_BACKUP}
        r = requests.post(url, files=files, data=data, timeout=60)
    r.raise_for_status()
    return r.json()


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
    if not allow_publish and not DRY_RUN:
        log(f"Post {post_id}: publicable, pero toca esperar el turno; queda pendiente.")
        return "deferred"

    local_images = []
    for i, url in enumerate(images):
        dest = tmpdir / f"{post_id}_{i}.jpg"
        download_image(url, dest)
        local_images.append(dest)

    edit = ask_claude(text, len(local_images))
    if edit.get("skip"):
        log(f"Post {post_id}: Claude decidió omitir ({edit.get('skip_reason')}).")
        return "skipped_by_ai"

    spec_path = build_compose_spec(local_images, edit, tmpdir)
    out_path = tmpdir / f"{post_id}_out.jpg"
    compose_image(spec_path, out_path)

    caption = edit.get("caption", "").strip()
    if not caption:
        caption = "#LCDLF6"

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
        )
        (preview_dir / f"{stub}.txt").write_text(details, encoding="utf-8")
        log(f"[DRY_RUN] Preview guardado: {preview_img.name}. Caption: {caption}")
        send_telegram_preview(out_path, caption, details, post_id)
        return "dry_run"

    result = publish_photo(out_path, caption)
    backup_post_id = result.get("post_id") or result.get("id")
    record_published(backup_post_id, post_id, text, caption)
    mark_published_now("auto")
    log(f"Post {post_id} -> publicado como {backup_post_id}")
    return "published"


def record_published(backup_post_id, source_post_id, source_text, caption):
    """Guarda el mapeo post-de-CAM1 -> post-original, para poder regenerar luego."""
    try:
        data = {}
        if PUBLISHED_MAP_PATH.exists():
            data = json.loads(PUBLISHED_MAP_PATH.read_text())
        data[str(backup_post_id)] = {
            "source_post_id": source_post_id,
            "source_text": source_text,
            "caption": caption,
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

    # Pausados: siguen en la cola y a la vista, pero no ocupan turno.
    congelados = [p for p in pendientes if p["id"] in pausados]
    if congelados:
        log(f"{len(congelados)} post(s) en pausa desde el panel; se saltan.")
    candidatos = [p for p in pendientes if p["id"] not in pausados]

    # "Publicar ahora": lo que marcaste se pone al frente de la fila.
    candidatos.sort(key=lambda p: (0 if p["id"] in prioridad else 1,
                                   p.get("created_time", "")))

    new_posts = candidatos[:MAX_POSTS_PER_RUN]

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

    if pendientes_restantes:
        log(f"Quedan {pendientes_restantes} post(s) en cola; se publicarán de a uno "
            f"cada {MIN_MINUTES_BETWEEN_POSTS:.0f} min.")


if __name__ == "__main__":
    main()
