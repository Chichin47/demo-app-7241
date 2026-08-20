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
import cola
import insta

BASE_DIR = Path(__file__).resolve().parent
OFFSET_PATH = BASE_DIR / "state" / "telegram_offset.json"
QUEUE_PATH = BASE_DIR / "state" / "telegram_queue.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Para el botón de reinicio: la misma llave que usa el turno para encolar su
# relevo. Si no está, el reinicio igual funciona (se cierra el turno y entra el
# relevo que ya quedó esperando en la puerta), solo que sin red de seguridad.
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()

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


def send_photo_url(chat_id, url, caption, reply_markup=None):
    """Manda una foto pasándole a Telegram la dirección de la imagen.

    La miniatura del panel es la foto ORIGINAL de la página 1, tal cual: no se
    descarga acá ni se compone nada. Telegram la trae solo. Si la dirección ya
    venció (las de Facebook caducan a las horas), se devuelve None y el que
    llama manda el texto sin foto.
    """
    try:
        kwargs = {"chat_id": chat_id, "photo": url, "caption": caption[:1024]}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        return api("sendPhoto", **kwargs)
    except Exception as e:
        log(f"No se pudo mandar la miniatura ({e}); va sin foto.")
        return None


def editar_ficha(chat_id, message_id, texto, reply_markup=None):
    """Actualiza una ficha del panel, sea foto (caption) o texto suelto."""
    markup = reply_markup or {"inline_keyboard": []}
    try:
        api("editMessageCaption", chat_id=chat_id, message_id=message_id,
            caption=texto[:1024], reply_markup=markup)
        return
    except Exception:
        pass
    try:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=texto[:4096], reply_markup=markup)
    except Exception as e:
        log(f"No se pudo actualizar la ficha {message_id}: {e}")


def borrar_mensaje(chat_id, message_id):
    try:
        api("deleteMessage", chat_id=chat_id, message_id=message_id)
        return True
    except Exception:
        return False  # ya borrado, o pasó de 48 h: no es un problema


def answer_callback(callback_id, text=""):
    """El globito que sale arriba del chat al tocar un botón.

    Ojo: esto falla seguido con un 400, y es esperable. El bot lee Telegram
    cada pocos minutos, así que cuando por fin contesta el globito ya venció
    ("query is too old"). No es grave: la respuesta de verdad es la ficha, que
    se edita aparte y esa sí llega siempre. Se anota el motivo que devuelve
    Telegram para no quedarse con un "400" pelado en el log.
    """
    try:
        api("answerCallbackQuery", callback_query_id=callback_id, text=text[:180])
    except Exception as e:
        detalle = getattr(getattr(e, "response", None), "text", "") or ""
        log(f"No se pudo responder el botón: {e} {detalle[:200]}".strip())


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
#   🗂 Cola           -> el panel: qué está por publicarse, con miniatura y
#                        botones por post (publicar ahora / pausar / eliminar).
#   🔎 Revisar ahora  -> corre el autochequeo completo (tokens de las dos
#                        páginas, posts pendientes, Claude, Telegram, ritmo)
#                        y devuelve el reporte al chat.
#   📊 Último post    -> qué fue lo último que publicó y hace cuánto.
#   ❔ Ayuda          -> recordatorio de cómo se usa.
#   🔄 Reiniciar      -> cierra el turno actual y arranca uno nuevo y limpio.
#
# También funcionan escritos como comando (/cola, /revisar, /ultimo, /ayuda,
# /reiniciar) y el teclado se vuelve a mandar solo si el usuario lo cerró.

BTN_COLA = "🗂 Cola"
BTN_REVISAR = "🔎 Revisar ahora"
BTN_ULTIMO = "📊 Último post"
BTN_PUBLICADOS = "🎞 Publicados"
BTN_AYUDA = "❔ Ayuda"
BTN_REINICIAR = "🔄 Reiniciar"

TECLADO_FIJO = {
    "keyboard": [
        [{"text": BTN_COLA}, {"text": BTN_REVISAR}],
        [{"text": BTN_ULTIMO}, {"text": BTN_PUBLICADOS}],
        [{"text": BTN_AYUDA}, {"text": BTN_REINICIAR}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

TEXTO_AYUDA = (
    "🤖 Así se maneja el bot:\n\n"
    "🗂 Cola — te muestro todo lo que está esperando para publicarse, con su "
    "miniatura para que lo reconozcas de un vistazo. Cada uno trae sus botones:\n"
    "   🚀 Publicar ahora — se salta la fila y sale en el próximo barrido.\n"
    "   ⏸ Pausar — se queda congelado sin salir, y los demás siguen normal.\n"
    "   🗑 Eliminar — no se publica nunca (el post sigue intacto en la página 1).\n"
    "   🖼 Foto / 🎬 Video — obliga a que ese salga en ese formato. Tocando el que "
    "ya está puesto lo sueltas y vuelve a decidir el bot.\n"
    "Si no tocas nada, todo sigue saliendo solo con su ritmo de siempre.\n\n"
    "🔎 Revisar ahora — revisa todo de punta a punta (las dos páginas, los posts "
    "pendientes, Claude, Telegram y el ritmo de publicación) y te manda el reporte "
    "acá mismo. Tarda unos segundos. No publica nada.\n\n"
    "📊 Último post — te dice qué fue lo último que publicó y hace cuánto.\n\n"
    "🎞 Publicados — la lista de lo que ya salió en la página 2. Cada uno trae "
    "el botón 🎬 Hacer video de este: sirve para cuando algo salió como foto y "
    "después ves que daba para video. El video se arma de nuevo desde el post "
    "original de la página 1 y se publica aparte, en el próximo barrido. La foto "
    "que ya está publicada NO se toca: si querés que quede solo el video, la "
    "borrás vos desde Facebook.\n\n"
    "🔄 Reiniciar — si algo se colgó, cierra el turno actual y arranca uno limpio. "
    "Te pregunta antes, para que no pase por accidente.\n\n"
    "📸 Para publicar algo a mano: mándame la foto con la descripción y te pregunto "
    "cómo la publico y a qué hora. Arriba de las horas tenés 🤖 Automático / "
    "🖼 Foto / 🎬 Video: elegí primero el formato y después la hora, porque tocar "
    "una hora cierra el menú. Igual, mientras no haya salido podés cambiarle el "
    "formato o la hora desde el mismo mensaje. Si no elegís nada queda en "
    "🤖 Automático, que ahora mira la marca: si el original de la página 1 lleva "
    "#UR sale en video, y si no lleva nada sale en foto.\n\n"
    "📌 Marca #topchefvip5 — si el original de la página 1 la lleva, ese post NO se "
    "publica en ningún lado. Se arma igual la imagen y la descripción, y te llegan "
    "acá al chat para que las uses donde quieras. Si además lleva #UR, también te "
    "mando el video. Las dos marcas se borran del texto: son órdenes para el bot, "
    "no parte de la descripción.\n\n"
    "El bot trabaja solo: barre la página 1 cada 3 minutos y publica de a uno, con "
    "al menos {min:.0f} minutos entre publicaciones. Por eso el panel puede estar "
    "hasta 3 minutos atrasado: con 🔄 Actualizar lo refrescas."
)


def normaliza(texto):
    """Quita tildes, emojis y mayúsculas para reconocer el botón o el comando."""
    txt = unicodedata.normalize("NFD", (texto or "").strip().lower())
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return "".join(c for c in txt if c.isalnum() or c.isspace()).strip()


def que_comando(texto):
    """Devuelve 'cola', 'revisar', 'ultimo', 'ayuda', 'reiniciar' o None."""
    t = normaliza(texto)
    if not t:
        return None
    # Coincidencia exacta para las palabras cortas: así «cola» abre el panel
    # pero «colaboración» en un texto suelto no dispara nada.
    if t in ("cola", "panel", "pendientes", "en espera", "espera"):
        return "cola"
    if t in ("reiniciar", "reinicio", "restart", "reset", "reiniciar bot"):
        return "reiniciar"
    if t.startswith("revisar") or t in ("estado", "chequeo", "autochequeo", "check"):
        return "revisar"
    if t.startswith("ultimo") or t in ("ultima", "ultima publicacion"):
        return "ultimo"
    if t.startswith("instagram") or t in ("ig", "insta"):
        return "instagram"
    if t.startswith("publicados") or t in ("publicado", "historial", "salidos"):
        return "publicados"
    if t.startswith("ayuda") or t in ("help", "comandos", "start", "menu"):
        return "ayuda"
    return None


def registrar_menu_comandos():
    """Deja los comandos en el menú '/' de Telegram (se hace una sola vez)."""
    try:
        api("setMyCommands", commands=json.dumps([
            {"command": "cola", "description": "Ver lo que está por publicarse"},
            {"command": "revisar", "description": "Revisar que todo esté bien"},
            {"command": "ultimo", "description": "Qué publicó último y hace cuánto"},
            {"command": "instagram", "description": "Si puede publicar en Instagram"},
            {"command": "publicados", "description": "Lo que ya salió (y pedir el video)"},
            {"command": "reiniciar", "description": "Cerrar el turno y arrancar uno limpio"},
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


def cmd_instagram(chat_id):
    """Dice si el bot puede publicar en Instagram, y si no, qué le falta.

    Está para no andar adivinando si al token le faltan permisos: se le
    pregunta a Facebook y Facebook contesta.
    """
    reply(chat_id, "📸 Preguntándole a Facebook…", TECLADO_FIJO)
    try:
        informe = insta.diagnostico(bot.PAGE_ID_BACKUP, bot.PAGE_TOKEN_BACKUP)
    except Exception as e:
        informe = f"❌ No se pudo revisar: {e}"
    reply(chat_id, informe, TECLADO_FIJO)


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
    origen = info.get("source_post_id")
    if origen:
        reply(chat_id, "¿Querés que además haga el video de este?",
              botones_publicado(origen, info))


# --------------------------------------------------------------------------
# 🎞 Publicados: pedir el video de algo que ya salió
# --------------------------------------------------------------------------
#
# El formato se elige antes de publicar, desde 🗂 Cola. Esto es para después:
# el post ya salió como foto y recién ahí ves que daba para video. El video se
# arma de nuevo desde el original de la página 1 y se publica aparte; la foto
# que ya está arriba no se toca.

# Cuántas publicaciones se muestran. Más que esto llena el chat de fichas.
PUBLICADOS_MAX = 15


def leer_publicados():
    """El registro de lo que se publicó, del más viejo al más nuevo."""
    try:
        if bot.PUBLISHED_MAP_PATH.exists():
            datos = json.loads(bot.PUBLISHED_MAP_PATH.read_text(encoding="utf-8"))
            return datos if isinstance(datos, dict) else {}
    except Exception as e:
        log(f"No pude leer el registro de publicaciones: {e}")
    return {}


def buscar_publicado(clave):
    """Devuelve (backup_id, info) del publicado cuyo original tiene esa clave."""
    for backup_id, info in leer_publicados().items():
        origen = info.get("source_post_id")
        if origen and cola.clave(origen) == clave:
            return backup_id, info
    return None, None


def botones_publicado(origen, info, backup_id=""):
    """Los botones de una ficha de publicado."""
    clave = cola.clave(origen)
    estado = cola.estado_video(origen)
    filas = []
    if estado == "pendiente":
        filas.append([{"text": "✖️ Cancelar el encargo",
                       "callback_data": f"v|can|{clave}"}])
    elif info.get("formato") == "reel":
        # Antes acá había un botón muerto que solo avisaba "ya salió como
        # video". Se cambió por uno que sirve: si el video salió mal y lo
        # borraste, con esto se rearma desde el ORIGINAL de la página 1 tal
        # como esté AHORA. O sea: corregís el texto en la página 1, tocás
        # este botón, y el video nuevo sale con el texto corregido.
        filas.append([{"text": "🔁 Rehacer el video (relee el original)",
                       "callback_data": f"v|new|{clave}"}])
        # Reenvía al chat el MISMO archivo que ya está publicado, sin
        # regenerar nada: lo baja de Facebook y lo manda por Telegram.
        filas.append([{"text": "📤 Mandar el video al chat",
                       "callback_data": f"v|snd|{clave}"}])
    else:
        filas.append([{"text": "🎬 Hacer video de este",
                       "callback_data": f"v|new|{clave}"}])
        # Y el gemelo en foto: para cuando la publicación salió mal o quedó
        # invisible y la borraste. Relee el original de la página 1 tal como
        # esté y la vuelve a publicar como foto.
        filas.append([{"text": "📷 Republicar como foto (relee el original)",
                       "callback_data": f"v|ftr|{clave}"}])
    if backup_id:
        filas.append([{"text": "👁 Ver la publicación",
                       "url": f"https://www.facebook.com/{backup_id}"}])
    return {"inline_keyboard": filas}


def ficha_publicado(n, backup_id, info):
    # n puede venir vacío: cuando se reescribe la ficha después de tocar un
    # botón no sabemos en qué puesto de la lista estaba, y poner un número
    # inventado confunde más que no poner ninguno.
    estado = cola.estado_video(info.get("source_post_id"))
    if info.get("formato") == "reel":
        salio = "🎬 salió como video"
    elif info.get("formato") == "foto":
        salio = "🖼 salió como foto"
    else:
        salio = "🖼 salió como foto (registro viejo)"

    cuando = info.get("when")
    try:
        edad = f" · {hace_cuanto(time.time() - float(cuando))}" if cuando else ""
    except (TypeError, ValueError):
        edad = ""
    lineas = [(f"{n}. " if n else "") + salio + edad]
    if estado == "pendiente":
        lineas.append("🎬 VIDEO PEDIDO — sale en el próximo barrido, sin esperar turno.")
    elif estado == "hecho":
        lineas.append("✅ El video de este ya se publicó.")
    elif estado == "error":
        lineas.append("⚠️ Intenté el video y no salió. Podés pedirlo de nuevo.")

    texto = (info.get("caption") or info.get("source_text") or "").strip()
    lineas.append(f"\n«{texto[:400]}»" if texto else "\n(sin texto)")
    return "\n".join(lineas)


def cmd_publicados(chat_id):
    """Lista lo último publicado, con el botón para encargar el video."""
    datos = leer_publicados()
    if not datos:
        reply(chat_id, "Todavía no hay ninguna publicación registrada.", TECLADO_FIJO)
        return

    ultimos = list(datos.items())[-PUBLICADOS_MAX:][::-1]   # el más nuevo arriba
    pendientes = len(cola.rehacer_pendientes())

    cabecera = f"🎞 PUBLICADOS — lo último que salió en la página 2 ({len(ultimos)})."
    if pendientes:
        cabecera += f"\n\n🎬 Tenés {pendientes} video(s) encargado(s) esperando el próximo barrido."
    cabecera += ("\n\nSi alguno salió como foto y querés el video, tocá 🎬 Hacer video "
                 "de este. Lo armo de nuevo desde el post original de la página 1 y lo "
                 "publico aparte. La foto que ya está arriba NO se toca ni se borra: "
                 "eso lo decidís vos.")
    reply(chat_id, cabecera, TECLADO_FIJO)

    for n, (backup_id, info) in enumerate(ultimos, start=1):
        origen = info.get("source_post_id")
        if not origen:
            continue
        reply(chat_id, ficha_publicado(n, backup_id, info),
              botones_publicado(origen, info, backup_id))
    log(f"Panel de publicados: {len(ultimos)} ficha(s).")


def handle_sin_dialogo_callback(cb, partes):
    """Botones de la consulta 🤔 SIN DIÁLOGO: cuando Claude no encontró diálogo
    en un post nuevo, el bot pregunta en vez de descartar. Elegir foto o video
    encarga por cola.pedir_video (el mismo camino que el panel de publicados),
    así que sale en el próximo barrido; "dejarlo fuera" simplemente cierra la
    ficha y el post queda descartado como antes."""
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    modo = partes[1] if len(partes) > 1 else ""

    if modo == "x":
        answer_callback(cb["id"], "Descartado.")
        editar_ficha(chat_id, message_id,
                     "🗑 Descartado: este post no sale en ningún lado.")
        return

    origen = partes[2] if len(partes) > 2 else ""
    if modo not in ("f", "v") or not origen:
        answer_callback(cb["id"], "No entendí ese botón.")
        return

    formato = "foto" if modo == "f" else "reel"
    ok, estado = cola.pedir_video(origen, formato=formato)
    if not ok and estado == "ya":
        answer_callback(cb["id"], "Ya estaba encargado.")
        editar_ficha(chat_id, message_id,
                     "⏳ Ya estaba encargado; sale en el próximo barrido.")
        return
    if not ok:
        answer_callback(cb["id"], "No se pudo anotar el encargo.")
        return
    que = "foto" if formato == "foto" else "video"
    answer_callback(cb["id"], f"Listo: sale como {que}.")
    editar_ficha(chat_id, message_id,
                 f"✅ Encargado como {que}: sale en el próximo barrido "
                 f"(~3 min) hacia la página 2 e Instagram.")
    log(f"Sin diálogo {origen}: encargado como {que} desde el chat.")


def handle_video_callback(cb, partes):
    """Botones de 🎞 Publicados: encargar o cancelar el video de algo ya salido."""
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    accion = partes[1] if len(partes) > 1 else ""
    clave = partes[2] if len(partes) > 2 else ""

    if accion == "yaes":
        answer_callback(cb["id"], "Este ya salió como video; no tiene sentido rehacerlo.")
        return

    backup_id, info = buscar_publicado(clave)
    if not info:
        answer_callback(cb["id"], "Ya no tengo el registro de esa publicación.")
        return
    origen = info.get("source_post_id")

    if accion == "snd":
        # Reenviar el video publicado al chat: va por la misma cola de
        # encargos, pero con formato "chat" (no publica nada, solo manda).
        ok, motivo = cola.pedir_video(origen, backup_id,
                                      info.get("source_text") or "",
                                      formato="chat")
        if motivo == "ya":
            answer_callback(cb["id"], "Ya estaba pedido; sale en el próximo barrido.")
        elif ok:
            answer_callback(cb["id"], "Listo: te mando el video en el próximo barrido (~3 min).")
            log(f"Publicados: reenvío al chat encargado para {origen}.")
        else:
            answer_callback(cb["id"], "No pude anotar el pedido; probá de nuevo.")
        return

    if accion in ("new", "ftr"):
        # Paso intermedio: antes de encargar se pregunta A DÓNDE va. Sirve
        # para las limpiezas a medias, cuando una de las dos copias quedó bien
        # y solo hay que reponer la otra.
        es_foto = accion == "ftr"
        que = "la foto" if es_foto else "el video"
        answer_callback(cb["id"], f"¿A dónde mando {que}?")
        editar_ficha(
            chat_id, message_id,
            f"¿A dónde va {que}?\n\nSi una de las dos copias ya está bien "
            f"publicada, elegí solo la que falta, así no se duplica.\n\n"
            f"«{(info.get('source_text') or '')[:250]}»",
            {"inline_keyboard": [
                [{"text": "🌐 Facebook e Instagram",
                  "callback_data": f"v|{'df' if es_foto else 'dv'}|{clave}|ambos"}],
                [{"text": "📘 Solo Facebook",
                  "callback_data": f"v|{'df' if es_foto else 'dv'}|{clave}|fb"}],
                [{"text": "📸 Solo Instagram",
                  "callback_data": f"v|{'df' if es_foto else 'dv'}|{clave}|ig"}],
                [{"text": "↩️ Mejor no", "callback_data": f"v|vol|{clave}"}],
            ]},
        )
        return
    elif accion in ("dv", "df"):
        destino = partes[3] if len(partes) > 3 else "ambos"
        formato = "foto" if accion == "df" else "reel"
        ok, motivo = cola.pedir_video(origen, backup_id,
                                      info.get("source_text") or "",
                                      formato=formato, destino=destino)
        nombre = {"ambos": "Facebook e Instagram", "fb": "solo Facebook",
                  "ig": "solo Instagram"}.get(destino, destino)
        if motivo == "ya":
            answer_callback(cb["id"], "Ese encargo ya estaba pedido.")
        elif ok:
            answer_callback(cb["id"], f"Listo: sale en el próximo barrido, {nombre}.")
            log(f"Publicados: {formato} encargado para {origen} -> {destino}.")
        else:
            answer_callback(cb["id"], "No pude anotar el encargo; probá de nuevo.")
            return
    elif accion == "vol":
        answer_callback(cb["id"], "Listo, no encargo nada.")
        editar_ficha(chat_id, message_id, ficha_publicado(None, backup_id, info),
                     botones_publicado(origen, info, backup_id))
        return
    elif accion == "can":
        cola.cancelar_video(origen)
        answer_callback(cb["id"], "Cancelado.")
        log(f"Publicados: encargo cancelado para {origen}.")
    else:
        answer_callback(cb["id"], "Botón no reconocido.")
        return

    editar_ficha(chat_id, message_id, ficha_publicado(None, backup_id, info),
                 botones_publicado(origen, info, backup_id))


def cmd_ayuda(chat_id):
    registrar_menu_comandos()
    reply(chat_id, TEXTO_AYUDA.format(min=bot.MIN_MINUTES_BETWEEN_POSTS), TECLADO_FIJO)


# --------------------------------------------------------------------------
# 🗂 Panel de cola
# --------------------------------------------------------------------------
#
# El panel dibuja una ficha por cada post que espera turno en la página 1:
# miniatura (la foto original, sin editar), cuándo se publicó allá, el texto
# original y en qué punto de la fila está. Debajo de cada ficha van sus
# botones.
#
# Nada de esto llama a Claude. La lista sale de state/cola_snapshot.json, que
# el bot escribe en cada barrido con lo que ya trajo de Facebook, y la
# miniatura la trae Telegram sola desde la dirección de la foto. Mirar la cola
# cuesta cero.

# Cuántas fichas se dibujan como máximo. Más que esto llena el chat y no
# aporta: a diez minutos por post, son casi dos horas de trabajo por delante.
PANEL_MAX_FICHAS = 8

ETIQUETA_DESCARTE = {
    "video": "🎬 Es un video — el bot no publica videos, así que este se salta solo.",
    "sin_foto": "🚫 No tiene foto — el bot solo republica posts con foto.",
    "sin_texto": "🚫 No tiene texto — sin texto no hay frase que resaltar.",
}


def hora_post(created_time):
    """Convierte la fecha que da Facebook a algo legible en hora local."""
    try:
        dt = datetime.strptime(created_time, "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return (created_time or "")[:16].replace("T", " ") or "sin fecha"
    loc = dt.astimezone(LOCAL_TZ)
    hoy = now_local().date()
    if loc.date() == hoy:
        return f"hoy {loc.strftime('%H:%M')}"
    if loc.date() == hoy - timedelta(days=1):
        return f"ayer {loc.strftime('%H:%M')}"
    return loc.strftime("%d/%m %H:%M")


def espera_estimada(posicion):
    """Cuántos minutos faltan para que salga el que está en esa posición.

    posicion 0 es el primero de la fila. El primero espera lo que le falte al
    espaciado mínimo desde la última publicación; cada uno de atrás suma otro
    espaciado completo.
    """
    minimo = bot.MIN_MINUTES_BETWEEN_POSTS
    desde = bot.minutes_since_last_publish()
    falta_el_primero = 0.0 if desde is None else max(0.0, minimo - desde)
    return falta_el_primero + posicion * minimo


def texto_espera(minutos):
    if minutos < 1:
        return "sale en el próximo barrido"
    if minutos < 60:
        return f"sale en ~{minutos:.0f} min"
    return f"sale en ~{minutos / 60:.1f} h"


def ficha_item(item, n, estado, posicion):
    """Arma el texto de una ficha del panel."""
    cabecera = f"{n}. 🕒 publicado en la página 1 {hora_post(item.get('created_time'))}"
    fotos = item.get("n_fotos") or 0
    if fotos > 1:
        cabecera += f" · {fotos} fotos"

    if estado == "prioridad":
        linea = "🚀 LO PEDISTE PRIMERO — sale en el próximo barrido, saltándose la espera."
    elif estado == "pausado":
        linea = "⏸ EN PAUSA — no va a salir hasta que le des ▶️ Reanudar. Los demás siguen normal."
    elif estado in ETIQUETA_DESCARTE:
        linea = ETIQUETA_DESCARTE[estado]
    else:
        linea = f"🕐 En espera, puesto {posicion + 1} de la fila — {texto_espera(espera_estimada(posicion))}."

    texto = cola.item_a_texto(item, 500)
    cuerpo = f"\n\n«{texto}»" if texto else "\n\n(sin texto)"
    return f"{cabecera}\n{linea}{cuerpo}"


def botones_item(item, estado, formato=None):
    clave = item.get("clave", "")
    publicable = estado in ("espera", "pausado", "prioridad")
    filas = []
    if publicable:
        fila = []
        if estado != "prioridad":
            fila.append({"text": "🚀 Publicar ahora", "callback_data": f"k|pub|{clave}"})
        if estado == "pausado":
            fila.append({"text": "▶️ Reanudar", "callback_data": f"k|rea|{clave}"})
        else:
            fila.append({"text": "⏸ Pausar", "callback_data": f"k|pau|{clave}"})
        filas.append(fila)
        # Formato: el que está elegido se marca con un punto. Tocar el que ya
        # está puesto lo suelta y el bot vuelve a decidir solo.
        filas.append([
            {"text": ("🔘 " if formato == "foto" else "") + "🖼 Foto",
             "callback_data": f"k|fot|{clave}"},
            {"text": ("🔘 " if formato == "reel" else "") + "🎬 Video",
             "callback_data": f"k|vid|{clave}"},
        ])
    filas.append([{"text": "🗑 Eliminar de la cola", "callback_data": f"k|del|{clave}"}])
    return {"inline_keyboard": filas}


def borrar_panel_anterior():
    """Limpia el panel dibujado la vez pasada, para no apilar paneles viejos."""
    panel = cola.leer_panel()
    chat = panel.get("chat_id")
    mensajes = panel.get("mensajes") or []
    if not chat or not mensajes:
        return
    borrados = sum(1 for mid in mensajes if borrar_mensaje(chat, mid))
    log(f"Panel anterior: {borrados}/{len(mensajes)} mensajes borrados.")


def teclado_actualizar():
    return {"inline_keyboard": [[{"text": "🔄 Actualizar", "callback_data": "k|ref|-"}]]}


def cmd_cola(chat_id):
    """Dibuja el panel de la cola: una ficha con botones por cada pendiente."""
    snap = cola.leer_snapshot()
    edad = cola.edad_snapshot(snap)
    ctrl = cola.leer_control()
    pausados = set(ctrl.get("pausados") or [])
    prioridad = set(ctrl.get("prioridad") or [])
    eliminados = set(ctrl.get("eliminados") or [])

    borrar_panel_anterior()

    if edad is None:
        reply(chat_id,
              "🗂 Todavía no tengo la lista armada.\n\n"
              "El bot la escribe en su próximo barrido; en 3 minutos como mucho "
              "vuelve a tocar 🗂 Cola y ya la tendrás.",
              TECLADO_FIJO)
        return

    items = [i for i in (snap.get("items") or []) if i.get("id") not in eliminados]

    def estado_de(item):
        if item.get("estado") != "listo":
            return item.get("estado", "sin_foto")
        if item.get("id") in prioridad:
            return "prioridad"
        if item.get("id") in pausados:
            return "pausado"
        return "espera"

    # Orden: primero lo que pediste que saliera ya, después por antigüedad.
    # Es el mismo orden en el que el bot los va a publicar.
    items.sort(key=lambda i: (0 if i.get("id") in prioridad else 1,
                              i.get("created_time", "")))

    estados = [estado_de(i) for i in items]
    en_fila = [e for e in estados if e in ("espera", "prioridad")]
    n_pausa = estados.count("pausado")
    n_descarte = len(estados) - len(en_fila) - n_pausa

    aviso_viejo = ""
    if edad > cola.FRESCA_MINUTOS * 60:
        aviso_viejo = (f"\n\n⚠️ Ojo: esta lista es de {hace_cuanto(edad)}. Si sigue "
                       "igual dentro de un rato, el bot puede estar caído: usa 🔎 Revisar ahora.")

    if not items:
        reply(chat_id,
              f"🗂 COLA — no hay nada esperando.\n\n"
              f"Todo lo de la página 1 ya está publicado o descartado. "
              f"En cuanto subas algo nuevo allá, aparece acá."
              + aviso_viejo,
              TECLADO_FIJO)
        reply(chat_id, "Lista al día de " + hace_cuanto(edad) + ".", teclado_actualizar())
        return

    partes = [f"🗂 COLA — {len(en_fila)} esperando turno"]
    if n_pausa:
        partes.append(f"{n_pausa} en pausa")
    if n_descarte:
        partes.append(f"{n_descarte} que el bot va a saltarse")
    cabecera = " · ".join(partes)

    desde = bot.minutes_since_last_publish()
    if en_fila:
        cabecera += (f"\n\nSalen de a uno cada {bot.MIN_MINUTES_BETWEEN_POSTS:.0f} min. "
                     f"El próximo {texto_espera(espera_estimada(0))}.")
    if desde is not None:
        cabecera += f"\nÚltima publicación {hace_cuanto(desde * 60)}."
    cabecera += ("\n\nSi no tocas nada, todo sigue saliendo solo en este orden."
                 + aviso_viejo)

    ids = []
    res = reply(chat_id, cabecera, TECLADO_FIJO)
    if res.get("result", {}).get("message_id"):
        ids.append(res["result"]["message_id"])

    posicion = 0
    for n, (item, estado) in enumerate(zip(items, estados), start=1):
        if n > PANEL_MAX_FICHAS:
            break
        texto = ficha_item(item, n, estado, posicion)
        if estado in ("espera", "prioridad"):
            posicion += 1
        markup = botones_item(item, estado, cola.formato_pedido(item.get("id")))
        res = send_photo_url(chat_id, item.get("foto") or "", texto, markup) \
            if item.get("foto") else None
        if not res:
            res = reply(chat_id, texto, markup)
        mid = res.get("result", {}).get("message_id")
        if mid:
            ids.append(mid)

    pie = f"Lista al día de {hace_cuanto(edad)}."
    if len(items) > PANEL_MAX_FICHAS:
        pie = (f"Muestro los primeros {PANEL_MAX_FICHAS}; hay "
               f"{len(items) - PANEL_MAX_FICHAS} más atrás en la fila.\n" + pie)
    res = reply(chat_id, pie, teclado_actualizar())
    if res.get("result", {}).get("message_id"):
        ids.append(res["result"]["message_id"])

    cola.guardar_panel(chat_id, ids)
    log(f"Panel dibujado: {len(items)} pendiente(s), {len(ids)} mensaje(s).")


# --------------------------------------------------------------------------
# 🔄 Reinicio
# --------------------------------------------------------------------------

def cmd_reiniciar(chat_id):
    reply(chat_id,
          "🔄 ¿Reinicio el bot?\n\n"
          "Cierro el turno que está corriendo ahora y arranca uno nuevo y limpio.\n\n"
          "No se pierde nada: lo ya publicado queda registrado, la cola sigue igual "
          "y lo que tengas agendado a mano respeta su hora.\n\n"
          "Tarda 1 o 2 minutos en volver; te llega el aviso 🟢 cuando ya esté arriba.",
          {"inline_keyboard": [[
              {"text": "✅ Sí, reiniciar", "callback_data": "rst|si"},
              {"text": "❌ No, dejalo así", "callback_data": "rst|no"},
          ]]})


def pedir_turno_nuevo():
    """Le pide a GitHub un turno nuevo. Devuelve qué contarle al usuario."""
    if not RELAY_TOKEN or not GITHUB_REPO:
        return "Cierro este turno y entra el relevo que ya estaba esperando."
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/ci.yml/dispatches",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {RELAY_TOKEN}",
            },
            json={"ref": "main"},
            timeout=30,
        )
    except Exception as e:
        log(f"No se pudo pedir turno nuevo: {e}")
        return ("No pude avisarle a GitHub, pero igual cierro este turno y entra "
                "el relevo que ya estaba esperando.")
    if r.status_code == 204:
        return "Turno nuevo pedido a GitHub y este se cierra en seguida."
    log(f"Dispatch de reinicio devolvió {r.status_code}.")
    return (f"GitHub respondió {r.status_code} al pedido de turno nuevo, pero igual "
            "cierro este turno y entra el relevo que ya estaba esperando.")


# --------------------------------------------------------------------------

def atender_comandos(mensajes):
    """Saca los mensajes que son comandos y los atiende. Devuelve el resto."""
    acciones = {
        "cola": cmd_cola,
        "revisar": cmd_revisar,
        "ultimo": cmd_ultimo,
        "instagram": cmd_instagram,
        "publicados": cmd_publicados,
        "reiniciar": cmd_reiniciar,
        "ayuda": cmd_ayuda,
    }
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
            acciones.get(cmd, cmd_ayuda)(chat_id)
        except Exception as e:
            log(f"ERROR atendiendo el comando {cmd}: {e}")
            reply(chat_id, f"❌ Algo falló atendiendo «{texto}»: {e}")
    return restantes


# --------------------------------------------------------------------------
# Menús
# --------------------------------------------------------------------------

# Cómo puede salir un envío hecho a mano. El valor vacío es "no elegí nada":
# entonces se mira la marca #UR en el texto, igual que en el automático.
FORMATOS_MANUAL = [
    ("🤖 Automático", ""),
    ("🖼 Foto", "foto"),
    ("🎬 Video", "reel"),
]


def etiqueta_formato(formato):
    if formato == "reel":
        return "🎬 Va a salir como video."
    if formato == "foto":
        return "🖼 Va a salir como foto."
    return "🤖 Lo decide la marca: con #UR sale video, sin #UR sale foto."


def fila_formato(key, elegido=""):
    """Los tres botones de formato, con un ✅ en el que está puesto."""
    return [
        {"text": ("✅ " if (elegido or "") == valor else "") + etiqueta,
         "callback_data": f"fmt|{key}|{valor}"}
        for etiqueta, valor in FORMATOS_MANUAL
    ]


def menu_rapido(key, formato=""):
    botones = [
        {"text": etiqueta, "callback_data": f"q|{key}|{mins}"}
        for etiqueta, mins in QUICK_DELAYS
    ]
    # El formato va arriba de las horas a propósito: primero decidís QUÉ sale y
    # después CUÁNDO, porque tocar una hora cierra el menú.
    filas = [fila_formato(key, formato), botones[0:3], botones[3:6], botones[6:]]
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


def _cabeza_envio(job):
    n = len(job.get("photos", []))
    resumen = (job.get("caption") or "")[:120]
    return (
        f"📸 Recibí {n} foto{'s' if n != 1 else ''} con esta descripción:\n\n"
        f"«{resumen}»\n\n"
        f"{etiqueta_formato(job.get('formato'))}\n\n"
    )


def texto_pendiente(job):
    return _cabeza_envio(job) + f"¿Cuándo la publico? (hora {TZ_LABEL})"


def texto_agendado(job):
    """Lo que se ve cuando ya elegiste la hora pero todavía no salió."""
    return _cabeza_envio(job) + (
        f"🗓 Sale el {fmt_local(job.get('publish_at', 0))} ({TZ_LABEL})."
    )


def menu_agendado(job):
    """Hasta que salga se puede cambiar el formato, la hora, o cancelarla."""
    key = job["key"]
    return {"inline_keyboard": [
        fila_formato(key, job.get("formato")),
        [{"text": "🕐 Cambiar la hora", "callback_data": f"back|{key}"},
         {"text": "❌ Cancelar", "callback_data": f"cancel|{key}"}],
    ]}


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

    # El guion hablado se le pide solo si este envío va a salir en video: o
    # porque tocaste 🎬, o porque el texto lleva la marca. Si va en foto, ese
    # pedido sería un guion que nadie va a escuchar, y se paga igual.
    pedido = job.get("formato")
    con_video = pedido == "reel" or (pedido != "foto" and bot.pide_video(text))

    log(f"{key}: {len(local_images)} foto(s) + descripción de {len(text)} caracteres. Pidiendo edición a Claude.")
    # manual=True: lo mandaste tú a propósito, así que Claude no puede descartarlo.
    edit = bot.ask_claude(text, len(local_images), manual=True, con_video=con_video)
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

    final_caption = bot.quitar_etiqueta(
        (edit.get("caption") or "").strip()) or "#LCDLF6"

    # Si el trabajo trae formato pedido a mano ("foto" o "reel"), manda eso, que
    # es lo normal cuando mandás desde aquí. Si no elegiste, se mira lo mismo
    # que en el automático: si el texto lleva #UR sale video, si no, foto. Si el
    # video falla en cualquier punto, se publica la foto: el post sale igual.
    guion = bot.guion_de_reel(edit, text) if con_video else None
    formato, motivo = bot.elegir_formato(
        guion, len(local_images), pedido, texto=text)
    log(f"{key}: sale como {formato} ({motivo}).")
    if formato == "reel":
        try:
            reel = bot.armar_reel(local_images, guion, tmpdir)
            backup_post_id = bot.publish_reel(reel, final_caption)
            bot.record_published(backup_post_id, key, text, final_caption)
            bot.mark_published_now("telegram")
            bot._anotar_formato("reel")
            # Los envíos a mano también corren la rueda de arranques: si no, un
            # video tuyo y uno automático podrían abrir igual.
            bot._anotar_arranque(guion.get("narracion") if guion else "")
            log(f"{key} -> publicado como reel {backup_post_id}")
            # El mismo archivo también a Instagram. Nunca frena esto: el reel
            # de Facebook ya salió y quedó anotado antes de llegar acá.
            ig_reel = None
            try:
                ig_reel = insta.publicar_reel(
                    bot.PAGE_ID_BACKUP, bot.PAGE_TOKEN_BACKUP,
                    backup_post_id, final_caption, reel, log=log)
            except Exception as e:
                log(f"Instagram quedó afuera esta vez ({e}); el reel ya salió.")
            reply(
                chat_id,
                f"✅ Publicado como video.\nID: {backup_post_id}"
                + ("\n\n📸 También salió en Instagram." if ig_reel else "")
                + f"\n\nDescripción usada:\n{final_caption}",
            )
            return
        except Exception as e:
            log(f"{key}: no salió el video ({e}); lo publico como foto.")
            motivo = f"falló el armado del video ({e})"

    result = bot.publish_photo(out_path, final_caption)
    backup_post_id = result.get("post_id") or result.get("id")
    bot.record_published(backup_post_id, key, text, final_caption)
    # Reloj compartido: evita que el bot automático publique justo detrás.
    bot.mark_published_now("telegram")
    bot._anotar_formato("foto")
    log(f"{key} -> publicado como {backup_post_id}")
    # Lo mismo que ya salió en la página, también a Instagram. Nunca frena esto:
    # si falla, se anota y listo, el post de Facebook ya está publicado.
    ig_post = None
    try:
        sueltas = []
        if not insta.forma(out_path, log=lambda *a: None)[0]:
            sueltas = bot.armar_diapositivas(local_images, edit, tmpdir)
        ig_post = insta.publicar_foto(
            bot.PAGE_ID_BACKUP, bot.PAGE_TOKEN_BACKUP, result, final_caption,
            ruta=out_path, diapositivas=sueltas, log=log)
    except Exception as e:
        log(f"Instagram quedó afuera esta vez ({e}); el post ya salió igual.")
    # Si pediste video y salió foto, hay que decirlo: si no, parece que el botón
    # no hizo nada. La foto se publica igual, nunca se pierde el post.
    aclaracion = ""
    if job.get("formato") == "reel":
        aclaracion = f"\n\n⚠️ Pediste video pero salió como foto: {motivo}."
    if ig_post:
        aclaracion += "\n\n📸 También salió en Instagram."
    reply(chat_id,
          f"✅ Publicado en la página.\nID: {backup_post_id}"
          f"{aclaracion}\n\nDescripción usada:\n{final_caption}")


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

def ya_salio(post_id):
    """¿Este post de la página 1 ya se publicó mientras mirabas el panel?

    El panel se dibuja con la foto de la cola, y esa foto se toma al principio
    del barrido, ANTES de publicar. Si justo en ese hueco salió el post, la
    ficha que tenés en pantalla quedó vieja: los botones siguen ahí, pero
    apretarlos no cambiaría nada porque el bot ya pasó por ese post y no vuelve.
    """
    try:
        return str(post_id) in set(bot.load_state().get("processed") or [])
    except Exception:
        return False


def handle_panel_callback(cb, partes):
    """Botones del panel de cola: publicar ahora, pausar, reanudar, eliminar."""
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    accion = partes[1] if len(partes) > 1 else ""
    clave = partes[2] if len(partes) > 2 else ""

    if accion == "ref":
        answer_callback(cb["id"], "Actualizando…")
        cmd_cola(chat_id)
        return

    snap = cola.leer_snapshot()
    item = next((i for i in (snap.get("items") or []) if i.get("clave") == clave), None)
    if not item:
        answer_callback(cb["id"], "Ese post ya no está en la cola; actualiza el panel.")
        return
    pid = item["id"]
    n = next((k for k, i in enumerate(snap.get("items") or [], start=1)
              if i.get("clave") == clave), 1)

    # Post que ya salió: se avisa y no se guarda nada. Antes el botón decía
    # "listo" y anotaba la orden, pero esa orden no la iba a leer nadie, así
    # que uno se quedaba esperando un video que nunca se iba a armar.
    if accion in ("pub", "pau", "rea", "fot", "vid", "del") and ya_salio(pid):
        answer_callback(cb["id"], "Ese post ya se publicó; el panel estaba viejo.")
        backup_id, info = buscar_publicado(clave)
        texto = ("✅ YA PUBLICADO — este post salió justo antes de que tocaras el "
                 "botón, así que el panel que estabas viendo ya estaba viejo y "
                 "esta elección no cambia nada.")
        filas = []
        # Si salió como foto y vos querías el video, no te mando a buscarlo:
        # el botón de 🎞 Publicados va acá mismo, con la misma clave.
        if info and info.get("formato") != "reel":
            texto += ("\n\nSi lo querías en video, tocá el botón de abajo: lo armo "
                      "de nuevo con el mismo texto y lo publico aparte. La foto que "
                      "ya está arriba no se toca.")
            filas.append([{"text": "🎬 Hacer video de este",
                           "callback_data": f"v|new|{clave}"}])
        if backup_id:
            filas.append([{"text": "👁 Ver la publicación",
                           "url": f"https://www.facebook.com/{backup_id}"}])
        filas.append([{"text": "🔄 Actualizar panel", "callback_data": "k|ref"}])
        editar_ficha(chat_id, message_id,
                     f"{texto}\n\n«{cola.item_a_texto(item, 300)}»",
                     {"inline_keyboard": filas})
        log(f"Panel: {pid} ya estaba publicado; el botón «{accion}» no se aplicó.")
        return

    if accion == "pub":
        cola.marcar(pid, "prioridad", True)
        answer_callback(cb["id"], "Listo: sale primero, en el próximo barrido.")
        editar_ficha(chat_id, message_id, ficha_item(item, n, "prioridad", 0),
                     botones_item(item, "prioridad"))
        log(f"Panel: {pid} marcado para salir primero.")
        return

    if accion == "pau":
        cola.marcar(pid, "pausados", True)
        answer_callback(cb["id"], "Pausado. Los demás siguen normal.")
        editar_ficha(chat_id, message_id, ficha_item(item, n, "pausado", 0),
                     botones_item(item, "pausado"))
        log(f"Panel: {pid} pausado.")
        return

    if accion == "rea":
        cola.marcar(pid, "pausados", False)
        answer_callback(cb["id"], "Reanudado: vuelve a la fila.")
        editar_ficha(chat_id, message_id, ficha_item(item, n, "espera", 0),
                     botones_item(item, "espera"))
        log(f"Panel: {pid} reanudado.")
        return

    if accion in ("fot", "vid"):
        lista = "foto" if accion == "fot" else "video"
        puesto = cola.formato_pedido(pid)
        # Tocar el formato que ya estaba elegido lo suelta: vuelve a decidir el bot.
        quitar = (puesto == "foto" and accion == "fot") or (puesto == "reel" and accion == "vid")
        cola.marcar(pid, lista, not quitar)
        nuevo = cola.formato_pedido(pid)
        if nuevo == "reel":
            aviso = "Este sale como video."
        elif nuevo == "foto":
            aviso = "Este sale como foto."
        else:
            aviso = "Listo: el bot decide el formato."
        answer_callback(cb["id"], aviso)
        estado_actual = "prioridad" if pid in set(cola.leer_control().get("prioridad") or []) \
            else ("pausado" if pid in set(cola.leer_control().get("pausados") or []) else "espera")
        editar_ficha(chat_id, message_id, ficha_item(item, n, estado_actual, 0),
                     botones_item(item, estado_actual, nuevo))
        log(f"Panel: {pid} -> formato {nuevo or 'automático'}.")
        return

    if accion == "del":
        cola.marcar(pid, "eliminados", True)
        cola.marcar(pid, "pausados", False)
        cola.marcar(pid, "prioridad", False)
        answer_callback(cb["id"], "Eliminado de la cola.")
        editar_ficha(
            chat_id, message_id,
            f"🗑 ELIMINADO de la cola — este post no se va a publicar.\n\n"
            f"En la página 1 sigue intacto; lo único que pasa es que el bot ya no "
            f"lo va a republicar.\n\n«{cola.item_a_texto(item, 300)}»",
            {"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"k|und|{clave}"}]]},
        )
        log(f"Panel: {pid} eliminado de la cola.")
        return

    if accion == "und":
        cola.marcar(pid, "eliminados", False)
        answer_callback(cb["id"], "Deshecho: vuelve a la cola.")
        editar_ficha(chat_id, message_id, ficha_item(item, n, "espera", 0),
                     botones_item(item, "espera"))
        log(f"Panel: {pid} devuelto a la cola.")
        return

    answer_callback(cb["id"], "Botón no reconocido.")


def handle_reinicio_callback(cb, partes):
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    if (partes[1] if len(partes) > 1 else "") != "si":
        answer_callback(cb["id"], "Cancelado.")
        edit_message(chat_id, message_id, "Listo, no reinicié nada. Todo sigue igual.")
        return
    answer_callback(cb["id"], "Reiniciando…")
    detalle = pedir_turno_nuevo()
    cola.pedir_reinicio("telegram")
    edit_message(chat_id, message_id,
                 "🔄 Reiniciando.\n\n" + detalle +
                 "\n\nEn 1 o 2 minutos te llega el aviso 🟢 de que ya arrancó.")
    log("Reinicio pedido desde el chat.")


def handle_callback(cb, jobs):
    data = cb.get("data") or ""
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    partes = data.split("|")
    accion = partes[0]

    # Los botones del panel de cola y del reinicio se atienden aparte: no
    # tienen nada que ver con la cola de envíos manuales de más abajo.
    if accion == "k":
        handle_panel_callback(cb, partes)
        return
    if accion == "rst":
        handle_reinicio_callback(cb, partes)
        return
    if accion == "v":
        handle_video_callback(cb, partes)
        return
    if accion == "s":
        handle_sin_dialogo_callback(cb, partes)
        return

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

    if accion == "fmt":
        valor = partes[2] if len(partes) > 2 else ""
        # Tocar el formato que ya estaba puesto lo suelta: vuelve a decidir el bot.
        if valor and job.get("formato") == valor:
            valor = ""
        job["formato"] = valor
        if valor == "reel":
            aviso = "Esta sale como video."
        elif valor == "foto":
            aviso = "Esta sale como foto."
        else:
            aviso = "Listo: el bot decide el formato."
        answer_callback(cb["id"], aviso)
        # Si ya tenía hora se redibuja la ficha de agendada, no el menú de horas:
        # cambiar el formato no debe borrarte la hora que ya elegiste.
        if job.get("status") == "scheduled":
            edit_message(chat_id, message_id, texto_agendado(job), menu_agendado(job))
        else:
            edit_message(chat_id, message_id, texto_pendiente(job), menu_rapido(key, valor))
        log(f"{key}: formato pedido a mano -> {valor or 'automático'}.")
        return

    if accion == "back":
        answer_callback(cb["id"])
        edit_message(chat_id, message_id, texto_pendiente(job),
                     menu_rapido(key, job.get("formato")))
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
        edit_message(chat_id, message_id, texto_agendado(job), menu_agendado(job))
        log(f"{key}: agendada para {fmt_local(job['publish_at'])}.")
        return

    if accion == "q":
        mins = int(partes[2])
        job["publish_at"] = time.time() + mins * 60
        job["status"] = "scheduled"
        answer_callback(cb["id"], "Listo.")
        if mins == 0:
            # Sale en esta misma pasada: ya no tiene sentido dejar botones para
            # cambiarle nada, porque para cuando los toques ya se publicó.
            edit_message(chat_id, message_id,
                         "🚀 Publicando ahora mismo…\n\n" + etiqueta_formato(job.get("formato")))
            log(f"{key}: publicar ahora ({job.get('formato') or 'automático'}).")
        else:
            edit_message(chat_id, message_id, texto_agendado(job), menu_agendado(job))
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
            # Vacío = lo decide la marca: con #UR video, sin #UR foto.
            # Se llena si tocás 🖼 Foto o 🎬 Video en el menú.
            "formato": "",
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
