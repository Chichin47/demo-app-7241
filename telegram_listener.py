#!/usr/bin/env python3
"""Recibe posts armados a mano por Telegram, pregunta CUÁNDO publicarlos y los publica.

Caso de uso: a veces el bot automático no encuentra nada que publicar (no hay
posts nuevos en la página principal, o Claude decide omitir el que hay). Para
esos casos, el usuario le manda directamente al bot de Telegram la FOTO y la
DESCRIPCIÓN.

Flujo:

1. Llega la foto (con su descripción como caption, o el texto en un mensaje
   aparte; varias fotos juntas = un solo post).
2. El bot responde con un menú de botones para elegir CUÁNDO publicarla:
      Ahora · 1 min · 2 min · 3 min · 5 min · 15 min · 30 min · 📅 Más
   El botón "📅 Más" abre primero los días (Hoy, Mañana, ...) y después las
   horas de ese día.
3. Al tocar un botón, la publicación queda agendada en state/telegram_queue.json
   con su hora exacta. El bot confirma en el chat.
4. En cada corrida, las publicaciones cuya hora ya llegó se procesan igual que
   un post normal: Claude elige las frases resaltantes + censura + descripción
   alterna, se compone la imagen y se publica en la página de respaldo.
5. Se publica como máximo UNA por corrida, y se anota el reloj compartido con
   el bot automático para que nunca salgan dos posts pegados (eso es lo que
   Meta lee como comportamiento de bot).

Los envíos manuales respetan la hora que elegiste: no esperan la cola del
barrido automático de la página 1, y tienen prioridad sobre ella.

Mensajes de cualquier otro chat se ignoran (solo se loguean) por seguridad:
solo el chat configurado en TELEGRAM_CHAT_ID puede disparar publicaciones.
"""
import os
import sys
import json
import time
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import poll_and_publish as bot

BASE_DIR = Path(__file__).resolve().parent
OFFSET_PATH = BASE_DIR / "state" / "telegram_offset.json"
QUEUE_PATH = BASE_DIR / "state" / "telegram_queue.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Zona horaria para mostrar y agendar horas. GitHub Actions corre en UTC, así
# que todo lo que el usuario ve (botones de día/hora y confirmaciones) se
# convierte a esta zona. Se puede cambiar sin tocar código con la variable de
# repositorio TZ_OFFSET_HOURS (ej. -5 para Perú/Colombia, -6 para México/
# Centroamérica, -4 para Venezuela/Chile en verano).
TZ_OFFSET_HOURS = bot.env_num("TZ_OFFSET_HOURS", -6, float)
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
TZ_LABEL = f"UTC{int(TZ_OFFSET_HOURS):+d}"

# Botones rápidos del menú (etiqueta, minutos de espera).
QUICK_DELAYS = [
    ("Ahora", 0),
    ("1 min", 1),
    ("2 min", 2),
    ("3 min", 3),
    ("5 min", 5),
    ("15 min", 15),
    ("30 min", 30),
]

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

# Cuánto tiempo se conservan las entradas ya publicadas en la cola (horas).
QUEUE_KEEP_HOURS = 48


def log(msg):
    print(f"[tg] {msg}", flush=True)


def now_local():
    return datetime.now(LOCAL_TZ)


def fmt_local(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d/%m a las %H:%M")


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------

def load_offset():
    if OFFSET_PATH.exists():
        try:
            return json.loads(OFFSET_PATH.read_text()).get("offset", 0)
        except Exception:
            return 0
    return 0


def save_offset(offset):
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(json.dumps({"offset": offset}))


def load_queue():
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text()).get("jobs", [])
        except Exception:
            return []
    return []


def save_queue(jobs):
    corte = time.time() - QUEUE_KEEP_HOURS * 3600
    vivos = [
        j for j in jobs
        if j.get("status") != "done" or j.get("done_at", 0) > corte
    ]
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps({"jobs": vivos}, ensure_ascii=False, indent=2))


def find_job(jobs, key):
    for j in jobs:
        if j.get("key") == key:
            return j
    return None


# --------------------------------------------------------------------------
# API de Telegram
# --------------------------------------------------------------------------

def api(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    if isinstance(params.get("reply_markup"), (dict, list)):
        params["reply_markup"] = json.dumps(params["reply_markup"])
    r = requests.post(url, data=params, timeout=60)
    r.raise_for_status()
    return r.json()


def reply(chat_id, text, reply_markup=None):
    try:
        kwargs = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        return api("sendMessage", **kwargs)
    except Exception as e:
        log(f"No se pudo responder en Telegram: {e}")
        return {}


def edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        kwargs = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
        kwargs["reply_markup"] = reply_markup or {"inline_keyboard": []}
        api("editMessageText", **kwargs)
    except Exception as e:
        log(f"No se pudo editar el mensaje {message_id}: {e}")


def answer_callback(callback_id, text=""):
    try:
        api("answerCallbackQuery", callback_query_id=callback_id, text=text[:180])
    except Exception as e:
        log(f"No se pudo responder el botón: {e}")


def download_telegram_photo(file_id, dest):
    info = api("getFile", file_id=file_id)
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    dest.write_bytes(r.content)


# --------------------------------------------------------------------------
# Tablero de control: botones fijos del chat
# --------------------------------------------------------------------------
#
# Debajo de la caja de texto del chat queda un teclado fijo con tres botones.
# Son la forma de tener el bot controlado sin entrar a GitHub ni leer logs:
#
#   🔎 Revisar ahora  -> corre el autochequeo completo (tokens de las dos
#                        páginas, posts pendientes, Claude, Telegram, ritmo)
#                        y devuelve el reporte al chat.
#   📊 Último post    -> qué fue lo último que publicó y hace cuánto.
#   ❔ Ayuda          -> recordatorio de cómo se usa.
#
# También funcionan escritos como comando (/revisar, /ultimo, /ayuda) y el
# teclado se vuelve a mandar solo si el usuario lo cerró.

BTN_REVISAR = "🔎 Revisar ahora"
BTN_ULTIMO = "📊 Último post"
BTN_AYUDA = "❔ Ayuda"

TECLADO_FIJO = {
    "keyboard": [[{"text": BTN_REVISAR}], [{"text": BTN_ULTIMO}, {"text": BTN_AYUDA}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

TEXTO_AYUDA = (
    "🤖 Así se maneja el bot:\n\n"
    "🔎 Revisar ahora — revisa todo de punta a punta (las dos páginas, los posts "
    "pendientes, Claude, Telegram y el ritmo de publicación) y te manda el reporte "
    "acá mismo. Tarda unos segundos. No publica nada.\n\n"
    "📊 Último post — te dice qué fue lo último que publicó y hace cuánto.\n\n"
    "📸 Para publicar algo a mano: mándame la foto con la descripción y te pregunto "
    "a qué hora la publico.\n\n"
    "El bot trabaja solo: barre la página 1 cada 3 minutos y publica de a uno, con "
    "al menos {min:.0f} minutos entre publicaciones."
)


def normaliza(texto):
    """Quita tildes, emojis y mayúsculas para reconocer el botón o el comando."""
    txt = unicodedata.normalize("NFD", (texto or "").strip().lower())
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return "".join(c for c in txt if c.isalnum() or c.isspace()).strip()


def que_comando(texto):
    """Devuelve 'revisar', 'ultimo', 'ayuda' o None."""
    t = normaliza(texto)
    if not t:
        return None
    if t.startswith("revisar") or t in ("estado", "chequeo", "autochequeo", "check"):
        return "revisar"
    if t.startswith("ultimo") or t in ("ultima", "ultima publicacion"):
        return "ultimo"
    if t.startswith("ayuda") or t in ("help", "comandos", "start", "menu"):
        return "ayuda"
    return None


def registrar_menu_comandos():
    """Deja los comandos en el menú '/' de Telegram (se hace una sola vez)."""
    try:
        api("setMyCommands", commands=json.dumps([
            {"command": "revisar", "description": "Revisar que todo esté bien"},
            {"command": "ultimo", "description": "Qué publicó último y hace cuánto"},
            {"command": "ayuda", "description": "Cómo se usa el bot"},
        ]))
    except Exception as e:
        log(f"No se pudo registrar el menú de comandos: {e}")


def hace_cuanto(segundos):
    if segundos < 90:
        return "hace menos de un minuto"
    minutos = segundos / 60
    if minutos < 90:
        return f"hace {minutos:.0f} minutos"
    horas = minutos / 60
    if horas < 36:
        return f"hace {horas:.1f} horas"
    return f"hace {horas / 24:.1f} días"


def cmd_revisar(chat_id):
    """Corre el autochequeo completo. Él mismo manda el reporte al chat."""
    reply(chat_id, "🔎 Revisando todo, dame unos segundos…", TECLADO_FIJO)
    ruta = BASE_DIR / "selfcheck.py"
    if not ruta.exists():
        reply(chat_id, "❌ No encuentro el archivo de revisión (selfcheck.py).")
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(ruta)],
            cwd=str(BASE_DIR),
            timeout=300,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        reply(chat_id, "⚠️ La revisión se pasó de 5 minutos y la corté. Vuelve a intentar.")
        return
    except Exception as e:
        reply(chat_id, f"❌ No se pudo correr la revisión: {e}")
        return
    if proc.returncode != 0:
        cola = (proc.stderr or proc.stdout or "").strip().splitlines()
        detalle = cola[-1] if cola else "sin detalle"
        reply(chat_id, f"⚠️ La revisión terminó con error: {detalle[:300]}")
    log("Autochequeo pedido desde el chat: terminado.")


def cmd_ultimo(chat_id):
    """Muestra la última publicación registrada."""
    try:
        datos = {}
        if bot.PUBLISHED_MAP_PATH.exists():
            datos = json.loads(bot.PUBLISHED_MAP_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        reply(chat_id, f"❌ No pude leer el registro de publicaciones: {e}", TECLADO_FIJO)
        return
    if not datos:
        reply(chat_id, "Todavía no hay ninguna publicación registrada.", TECLADO_FIJO)
        return

    post_id, info = list(datos.items())[-1]
    partes = [f"📊 Última publicación\n\nID: {post_id}"]
    cuando = info.get("when")
    if cuando:
        partes.append(f"Publicada el {fmt_local(float(cuando))} ({hace_cuanto(time.time() - float(cuando))}).")
    caption = (info.get("caption") or "").strip()
    if caption:
        partes.append(f"\nDescripción usada:\n{caption[:900]}")
    partes.append(f"\nhttps://www.facebook.com/{post_id}")

    mins = bot.minutes_since_last_publish()
    if mins is not None:
        falta = bot.MIN_MINUTES_BETWEEN_POSTS - mins
        partes.append(
            f"\nRitmo: última salida hace {mins:.0f} min. "
            + ("Ya puede publicar la siguiente." if falta <= 0 else f"La siguiente puede salir en {falta:.0f} min.")
        )
    reply(chat_id, "\n".join(partes), TECLADO_FIJO)


def cmd_ayuda(chat_id):
    registrar_menu_comandos()
    reply(chat_id, TEXTO_AYUDA.format(min=bot.MIN_MINUTES_BETWEEN_POSTS), TECLADO_FIJO)


def atender_comandos(mensajes):
    """Saca los mensajes que son comandos y los atiende. Devuelve el resto."""
    restantes = []
    for msg in mensajes:
        texto = (msg.get("text") or "").strip()
        # Solo texto suelto puede ser comando: una foto con caption nunca lo es.
        cmd = que_comando(texto) if texto and not msg.get("photo") else None
        if not cmd:
            restantes.append(msg)
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        log(f"Comando recibido: {cmd}")
        try:
            if cmd == "revisar":
                cmd_revisar(chat_id)
            elif cmd == "ultimo":
                cmd_ultimo(chat_id)
            else:
                cmd_ayuda(chat_id)
        except Exception as e:
            log(f"ERROR atendiendo el comando {cmd}: {e}")
            reply(chat_id, f"❌ Algo falló atendiendo «{texto}»: {e}")
    return restantes


# --------------------------------------------------------------------------
# Menús
# --------------------------------------------------------------------------

def menu_rapido(key):
    botones = [
        {"text": etiqueta, "callback_data": f"q|{key}|{mins}"}
        for etiqueta, mins in QUICK_DELAYS
    ]
    filas = [botones[0:3], botones[3:6], botones[6:]]
    filas[-1].append({"text": "📅 Más", "callback_data": f"more|{key}"})
    filas.append([{"text": "❌ Cancelar", "callback_data": f"cancel|{key}"}])
    return {"inline_keyboard": filas}


def menu_dias(key):
    hoy = now_local().date()
    filas, fila = [], []
    for i in range(7):
        d = hoy + timedelta(days=i)
        if i == 0:
            etiqueta = "Hoy"
        elif i == 1:
            etiqueta = "Mañana"
        else:
            etiqueta = f"{DIAS[d.weekday()]} {d.day}"
        fila.append({"text": etiqueta, "callback_data": f"day|{key}|{d.strftime('%m-%d')}"})
        if len(fila) == 3:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    filas.append([{"text": "‹ Volver", "callback_data": f"back|{key}"}])
    return {"inline_keyboard": filas}


def menu_horas(key, md):
    filas, fila = [], []
    for h in range(24):
        fila.append({"text": f"{h:02d}:00", "callback_data": f"hr|{key}|{md}|{h:02d}"})
        if len(fila) == 4:
            filas.append(fila)
            fila = []
    filas.append([{"text": "‹ Volver a los días", "callback_data": f"more|{key}"}])
    return {"inline_keyboard": filas}


def fecha_desde_md(md):
    """Convierte 'MM-DD' en la próxima fecha con ese mes/día (hoy incluido)."""
    hoy = now_local().date()
    mes, dia = (int(x) for x in md.split("-"))
    for anio in (hoy.year, hoy.year + 1):
        try:
            f = datetime(anio, mes, dia, tzinfo=LOCAL_TZ).date()
        except ValueError:
            continue
        if f >= hoy:
            return f
    return hoy


def texto_pendiente(job):
    n = len(job.get("photos", []))
    resumen = (job.get("caption") or "")[:120]
    return (
        f"📸 Recibí {n} foto{'s' if n != 1 else ''} con esta descripción:\n\n"
        f"«{resumen}»\n\n"
        f"¿Cuándo la publico? (hora {TZ_LABEL})"
    )


# --------------------------------------------------------------------------
# Armado de trabajos a partir de los mensajes
# --------------------------------------------------------------------------

def collect_jobs(messages):
    """Arma la lista de publicaciones a partir de los mensajes del chat.

    Devuelve (grupos, textos_sueltos). Cada grupo es un dict con:
      key     -> identificador para el registro (telegram_<message_id>)
      caption -> la descripción original
      photos  -> lista de file_id (ya elegido el tamaño más grande de cada foto)

    Acepta la foto con el texto como caption, o la foto y el texto como dos
    mensajes separados en cualquier orden (empareja por cercanía). Un álbum
    (media_group_id) cuenta como un solo post.
    """
    groups = []
    by_media_group = {}
    loose_texts = []

    for idx, msg in enumerate(messages):
        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]
            caption = (msg.get("caption") or "").strip()
            mgid = msg.get("media_group_id")
            if mgid and mgid in by_media_group:
                g = by_media_group[mgid]
                g["photos"].append(file_id)
                if caption and not g["caption"]:
                    g["caption"] = caption
                continue
            g = {
                "key": f"telegram_{msg['message_id']}",
                "photos": [file_id],
                "caption": caption,
                "idx": idx,
            }
            groups.append(g)
            if mgid:
                by_media_group[mgid] = g
        elif (msg.get("text") or "").strip():
            loose_texts.append((idx, msg))

    used_texts = set()
    for g in groups:
        if g["caption"]:
            continue
        best = None
        best_dist = None
        for t_idx, t_msg in loose_texts:
            if t_idx in used_texts:
                continue
            dist = abs(t_idx - g["idx"])
            if best_dist is None or dist < best_dist:
                best, best_dist = (t_idx, t_msg), dist
        if best is not None:
            used_texts.add(best[0])
            g["caption"] = best[1]["text"].strip()

    leftover_texts = [m for i, m in loose_texts if i not in used_texts]
    return groups, leftover_texts


# --------------------------------------------------------------------------
# Publicación
# --------------------------------------------------------------------------

def publish_job(job, chat_id, tmpdir):
    key = job["key"]
    text = job.get("caption") or ""

    local_images = []
    for n, file_id in enumerate(job["photos"]):
        dest = tmpdir / f"{key}_{n}.jpg"
        download_telegram_photo(file_id, dest)
        local_images.append(dest)

    log(f"{key}: {len(local_images)} foto(s) + descripción de {len(text)} caracteres. Pidiendo edición a Claude.")
    # manual=True: lo mandaste tú a propósito, así que Claude no puede descartarlo.
    edit = bot.ask_claude(text, len(local_images), manual=True)
    if edit.get("skip") or not edit.get("lines"):
        motivo = edit.get("skip_reason", "no devolvió frases")
        log(f"{key}: Claude no devolvió frases ({motivo}); se usa el texto original como frase.")
        edit = {
            "skip": False,
            "lines": [{"image_index": 0, "text": text[:70].strip(), "color": "white"}],
            "caption": (edit.get("caption") or text).strip(),
        }

    spec_path = bot.build_compose_spec(local_images, edit, tmpdir)
    out_path = tmpdir / f"{key}_out.jpg"
    bot.compose_image(spec_path, out_path)

    final_caption = (edit.get("caption") or "").strip() or "#LCDLF6"
    result = bot.publish_photo(out_path, final_caption)
    backup_post_id = result.get("post_id") or result.get("id")
    bot.record_published(backup_post_id, key, text, final_caption)
    # Reloj compartido: evita que el bot automático publique justo detrás.
    bot.mark_published_now("telegram")
    log(f"{key} -> publicado como {backup_post_id}")
    reply(chat_id, f"✅ Publicado en la página.\nID: {backup_post_id}\n\nDescripción usada:\n{final_caption}")


def publicar_pendientes(jobs, tmpdir):
    """Publica los trabajos cuya hora ya llegó. Máximo uno por corrida."""
    ahora = time.time()
    listos = [
        j for j in jobs
        if j.get("status") == "scheduled" and j.get("publish_at", 0) <= ahora
    ]
    listos.sort(key=lambda j: j.get("publish_at", 0))

    if not listos:
        agendados = [j for j in jobs if j.get("status") == "scheduled"]
        if agendados:
            prox = min(j["publish_at"] for j in agendados)
            log(f"{len(agendados)} publicación(es) agendada(s); la próxima el {fmt_local(prox)}.")
        return False

    if len(listos) > 1:
        log(f"{len(listos)} publicaciones vencidas; sale una y el resto en las próximas corridas.")

    job = listos[0]
    chat = job.get("chat_id") or TELEGRAM_CHAT_ID
    try:
        publish_job(job, chat, tmpdir)
        job["status"] = "done"
        job["done_at"] = time.time()
    except Exception as e:
        log(f"ERROR publicando {job['key']}: {e}")
        job["intentos"] = job.get("intentos", 0) + 1
        if job["intentos"] >= 3:
            job["status"] = "done"
            job["done_at"] = time.time()
            reply(chat, f"⚠️ No se pudo publicar tu envío después de 3 intentos: {e}")
        else:
            reply(chat, f"⚠️ Hubo un error al publicar; lo reintento en la próxima corrida ({e}).")
    return True


# --------------------------------------------------------------------------
# Callbacks de los botones
# --------------------------------------------------------------------------

def handle_callback(cb, jobs):
    data = cb.get("data") or ""
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    partes = data.split("|")
    accion = partes[0]
    key = partes[1] if len(partes) > 1 else ""
    job = find_job(jobs, key)

    if not job or job.get("status") == "done":
        answer_callback(cb["id"], "Esa publicación ya no está en la cola.")
        edit_message(chat_id, message_id, "⏱ Esta publicación ya no está disponible.")
        return

    if accion == "cancel":
        job["status"] = "done"
        job["done_at"] = time.time()
        answer_callback(cb["id"], "Cancelada.")
        edit_message(chat_id, message_id, "❌ Cancelada, no se publica.")
        log(f"{key}: cancelada por el usuario.")
        return

    if accion == "back":
        answer_callback(cb["id"])
        edit_message(chat_id, message_id, texto_pendiente(job), menu_rapido(key))
        return

    if accion == "more":
        answer_callback(cb["id"])
        edit_message(chat_id, message_id,
                     f"📅 ¿Qué día la publico? (hora {TZ_LABEL})", menu_dias(key))
        return

    if accion == "day":
        answer_callback(cb["id"])
        md = partes[2]
        f = fecha_desde_md(md)
        edit_message(chat_id, message_id,
                     f"🕐 ¿A qué hora del {f.strftime('%d/%m')}? (hora {TZ_LABEL})",
                     menu_horas(key, md))
        return

    if accion == "hr":
        md, hh = partes[2], int(partes[3])
        f = fecha_desde_md(md)
        cuando = datetime(f.year, f.month, f.day, hh, 0, tzinfo=LOCAL_TZ)
        if cuando.timestamp() <= time.time():
            answer_callback(cb["id"], "Esa hora ya pasó; elige otra.")
            return
        job["publish_at"] = cuando.timestamp()
        job["status"] = "scheduled"
        answer_callback(cb["id"], "Agendada.")
        edit_message(chat_id, message_id,
                     f"🗓 Agendada para el {fmt_local(job['publish_at'])} ({TZ_LABEL}).")
        log(f"{key}: agendada para {fmt_local(job['publish_at'])}.")
        return

    if accion == "q":
        mins = int(partes[2])
        job["publish_at"] = time.time() + mins * 60
        job["status"] = "scheduled"
        answer_callback(cb["id"], "Listo.")
        if mins == 0:
            edit_message(chat_id, message_id, "🚀 Publicando ahora mismo…")
            log(f"{key}: publicar ahora.")
        else:
            edit_message(chat_id, message_id,
                         f"⏱ Agendada en {mins} min → {fmt_local(job['publish_at'])} ({TZ_LABEL}).")
            log(f"{key}: agendada en {mins} min ({fmt_local(job['publish_at'])}).")
        return

    answer_callback(cb["id"], "Botón no reconocido.")


# --------------------------------------------------------------------------

def confirm_updates(offset):
    """Avanza el offset del lado de Telegram para no reprocesar nada."""
    try:
        api("getUpdates", offset=offset, timeout=0, limit=1)
        log(f"Mensajes confirmados en Telegram (offset {offset}).")
    except Exception as e:
        log(f"No se pudo confirmar el offset en Telegram: {e}")


def main():
    log(f"Iniciando revisión (token: {'sí' if TELEGRAM_BOT_TOKEN else 'no'}, "
        f"chat: {'sí' if TELEGRAM_CHAT_ID else 'no'}, zona: {TZ_LABEL}).")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram no configurado (faltan TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID); se omite.")
        return

    jobs = load_queue()
    offset = load_offset()

    try:
        data = api("getUpdates", offset=offset, timeout=0)
        updates = data.get("result", [])
    except Exception as e:
        log(f"No se pudo leer getUpdates: {e}")
        updates = []

    log(f"Offset {offset}: {len(updates)} novedad(es) pendiente(s).")

    nuevos_mensajes = []
    callbacks = []
    max_update_id = offset - 1
    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        cb = update.get("callback_query")
        if cb:
            chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
            if chat_id == TELEGRAM_CHAT_ID:
                callbacks.append(cb)
            else:
                log(f"Botón de chat no autorizado ({chat_id}), se ignora.")
            continue
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != TELEGRAM_CHAT_ID:
            log(f"Mensaje de chat no autorizado ({chat_id}), se ignora.")
            continue
        nuevos_mensajes.append(msg)

    if updates:
        next_offset = max_update_id + 1
        # Confirmamos ANTES de trabajar: si algo falla a mitad, preferimos no
        # arriesgarnos a duplicar. Lo que ya quedó agendado vive en la cola.
        confirm_updates(next_offset)
        save_offset(next_offset)

    # 1. Botones tocados: fijan la hora de publicación.
    for cb in callbacks:
        try:
            handle_callback(cb, jobs)
        except Exception as e:
            log(f"ERROR procesando botón: {e}")

    # 2. Comandos del tablero (🔎 Revisar ahora, 📊 Último post, ❔ Ayuda).
    #    Se atienden antes de armar publicaciones para que un comando nunca se
    #    confunda con la descripción de una foto.
    nuevos_mensajes = atender_comandos(nuevos_mensajes)

    # 3. Fotos nuevas: se encolan y se pregunta cuándo publicarlas.
    grupos, textos_sueltos = collect_jobs(nuevos_mensajes)
    for g in grupos:
        if not g["caption"]:
            log(f"{g['key']}: foto sin descripción y sin texto que emparejar; se avisa al usuario.")
            reply(
                TELEGRAM_CHAT_ID,
                "Recibí la foto pero no encontré la descripción. Mándame la foto y el "
                "texto juntos (el texto como descripción de la foto), o los dos mensajes "
                "seguidos y yo los junto.",
            )
            continue
        job = {
            "key": g["key"],
            "chat_id": TELEGRAM_CHAT_ID,
            "photos": g["photos"],
            "caption": g["caption"],
            "status": "awaiting",
            "publish_at": 0,
            "created_at": time.time(),
        }
        jobs.append(job)
        res = reply(TELEGRAM_CHAT_ID, texto_pendiente(job), menu_rapido(job["key"]))
        job["menu_msg_id"] = res.get("result", {}).get("message_id")
        log(f"{job['key']}: encolada, esperando que elijas la hora.")

    for msg in textos_sueltos:
        log(f"Texto sin foto (mensaje {msg['message_id']}); se avisa al usuario.")
        reply(
            TELEGRAM_CHAT_ID,
            "Recibí el texto pero no la foto. Mándame la foto junto con la "
            "descripción (o la foto justo después del texto) y la publico.",
        )

    # 4. Publicar lo que ya venció.
    with tempfile.TemporaryDirectory() as tmp:
        publicar_pendientes(jobs, Path(tmp))

    esperando = sum(1 for j in jobs if j.get("status") == "awaiting")
    if esperando:
        log(f"{esperando} publicación(es) esperando que elijas la hora en el chat.")

    save_queue(jobs)
    log("Revisión de Telegram terminada.")


if __name__ == "__main__":
    main()
