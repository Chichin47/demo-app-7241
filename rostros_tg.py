#!/usr/bin/env python3
"""Comandos de Telegram para registrar y reconocer participantes.

Lo llama telegram_listener.py en cada revisión, ANTES de armar publicaciones,
para que las fotos de registro nunca terminen en la cola de publicar.

Comandos (con o sin barra, con o sin tilde):

  /participante Pascal   + fotos  -> guarda la cara de Pascal. Las fotos pueden
                                     ir en el mismo mensaje (con ese texto como
                                     descripción), en un álbum, o en mensajes
                                     siguientes sin descripción (durante 15 min
                                     o hasta /listo).
  /participantes                  -> quiénes están registrados y con cuántas fotos.
  /olvidar Pascal                 -> borra sus huellas (pide confirmación).
  /quien                + foto    -> dice quién es quién en la foto, con un
                                     recuadro numerado por cara.
  «2 es Kevyn»                    -> después de un /quien: la cara 2 es Kevyn;
                                     la suma como referencia (así aprende).
  /listo                          -> termina el registro en curso.

Mientras hay un registro o un /quien abierto, las fotos SIN descripción van a
esto. Las fotos CON descripción siguen siendo publicaciones, como siempre.
"""
import json
import re
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PENDIENTE_PATH = BASE_DIR / "state" / "rostros_pendiente.json"
ULTIMO_PATH = BASE_DIR / "state" / "rostros_ultimo.json"
VENTANA_SEG = 15 * 60          # cuánto queda abierto un registro / un /quien
ULTIMO_VIGENCIA_SEG = 3 * 3600  # hasta cuándo vale «2 es Kevyn»

_RE_CMD = re.compile(r"^/?\s*(participantes|participante|olvidar|quien|quién|listo)\b(.*)$",
                     re.IGNORECASE | re.DOTALL)
_RE_CORRIGE = re.compile(r"^\s*(?:el|la|cara)?\s*(\d{1,2})\s+es\s+(?:el|la)?\s*(.+?)\s*[.!]*\s*$",
                         re.IGNORECASE)


def _log(tg, msg):
    tg.log(f"[rostros] {msg}")


# --------------------------------------------------------------------------
# Estado chico (sin datos biométricos)
# --------------------------------------------------------------------------

def _leer(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _escribir(path, datos):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")


def pendiente():
    p = _leer(PENDIENTE_PATH)
    if p.get("hasta", 0) > time.time():
        return p
    return {}


def abrir_pendiente(modo, nombre=""):
    _escribir(PENDIENTE_PATH, {"modo": modo, "nombre": nombre,
                               "hasta": time.time() + VENTANA_SEG})


def cerrar_pendiente():
    _escribir(PENDIENTE_PATH, {})


# --------------------------------------------------------------------------
# Ayudas de Telegram
# --------------------------------------------------------------------------

def _comando(texto):
    """('participante', 'Pascal') o (None, '')."""
    m = _RE_CMD.match((texto or "").strip())
    if not m:
        return None, ""
    cmd = m.group(1).lower().replace("é", "e")
    arg = " ".join(m.group(2).split())
    if cmd == "quien" and arg.lower().startswith("es"):
        arg = arg[2:].strip()
    return cmd, arg


def _archivo_imagen(msg):
    """file_id de la foto del mensaje (foto comprimida o imagen como archivo)."""
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"]
    doc = msg.get("document") or {}
    if (doc.get("mime_type") or "").startswith("image/"):
        return doc.get("file_id")
    return None


def enviar_foto(tg, chat_id, ruta, caption, reply_markup=None):
    import requests
    datos = {"chat_id": chat_id, "caption": caption[:1024]}
    if reply_markup:
        datos["reply_markup"] = json.dumps(reply_markup)
    try:
        with open(ruta, "rb") as fh:
            r = requests.post(
                f"https://api.telegram.org/bot{tg.TELEGRAM_BOT_TOKEN}/sendPhoto",
                data=datos, files={"photo": fh}, timeout=120)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        _log(tg, f"No se pudo mandar la foto: {e}; va solo el texto.")
        return tg.reply(chat_id, caption, reply_markup)


def _bajar(tg, file_ids, carpeta):
    rutas = []
    for i, fid in enumerate(file_ids, 1):
        destino = carpeta / f"f{i}.jpg"
        try:
            tg.download_telegram_photo(fid, destino)
            rutas.append(destino)
        except Exception as e:  # noqa: BLE001
            _log(tg, f"No se pudo bajar una foto: {e}")
    return rutas


# --------------------------------------------------------------------------
# Acciones
# --------------------------------------------------------------------------

def _texto_registro(res):
    lineas = []
    if res["guardadas"]:
        lineas.append(f"✅ {res['nombre']}: guardé {res['guardadas']} cara(s) nueva(s). "
                      f"Ya tiene {res['total']} en total.")
    else:
        lineas.append(f"⚠️ {res['nombre']}: no guardé ninguna cara nueva.")
    if res["sin_cara"]:
        lineas.append("• Sin cara que se reconozca: foto " + ", ".join(map(str, res["sin_cara"])) + ".")
    if res["repetidas"]:
        lineas.append("• Repetidas (ya las tenía): foto " + ", ".join(map(str, res["repetidas"])) + ".")
    for a in res["avisos"]:
        lineas.append(f"• {a}")
    if res.get("aviso_base"):
        lineas.append(f"⚠️ Ojo: {res['aviso_base']}.")
    total = res.get("total", 0)
    if 0 < total < 5:
        lineas.append("\nTe recomiendo llegar a 5-8 fotos distintas (de frente, de "
                      "perfil, con y sin gorra). Seguí mandando fotos sin descripción, "
                      "o escribí /listo.")
    else:
        lineas.append("\nPodés seguir mandando fotos o escribir /listo. "
                      "Para probar: /quien y después una foto.")
    return "\n".join(lineas)


def _registrar(tg, chat_id, nombre, file_ids):
    import rostros
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rutas = _bajar(tg, file_ids, tmp)
        if not rutas:
            tg.reply(chat_id, "No pude bajar las fotos de Telegram. Probá mandarlas de nuevo.")
            return
        try:
            res = rostros.registrar(nombre, rutas)
        except Exception as e:  # noqa: BLE001
            _log(tg, f"ERROR registrando a {nombre}: {e}")
            tg.reply(chat_id, f"❌ No pude registrar a {nombre}: {e}")
            return
        _log(tg, f"Registro de {res['nombre']}: {res['guardadas']} nueva(s), total {res['total']}.")
        texto = _texto_registro(res)
        hoja = rostros.hoja_contactos(res["recortes"], str(tmp / "hoja.jpg"))
        if hoja:
            enviar_foto(tg, chat_id, hoja, texto)
        else:
            tg.reply(chat_id, texto)


def _quien(tg, chat_id, file_ids):
    import rostros
    db = rostros.cargar()
    if not db["personas"]:
        tg.reply(chat_id, "Todavía no registré a nadie. Empezá con "
                          "/participante Nombre y mandame sus fotos.")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rutas = _bajar(tg, file_ids, tmp)
        ultimo = {"t": time.time(), "caras": []}
        for n_foto, ruta in enumerate(rutas, 1):
            try:
                img = rostros.leer_imagen(ruta)
                caras = rostros.reconocer(img, db)
            except Exception as e:  # noqa: BLE001
                _log(tg, f"ERROR reconociendo: {e}")
                tg.reply(chat_id, f"❌ No pude analizar la foto {n_foto}: {e}")
                continue
            if not caras:
                tg.reply(chat_id, f"🤷 En la foto {n_foto} no encontré ninguna cara "
                                  "(o salen muy chicas).")
                continue
            # Numeradas en orden de lectura (de arriba abajo, de izquierda a
            # derecha), que es como las va a nombrar quien mira la foto.
            alto = img.shape[0]
            caras.sort(key=lambda c: (round((c["caja"][1] + c["caja"][3] / 2) / (alto * 0.2)),
                                      c["caja"][0] + c["caja"][2] / 2))
            base = len(ultimo["caras"])
            lineas = []
            for i, c in enumerate(caras, base + 1):
                if c["nombre"] and c["confianza"] == "seguro":
                    lineas.append(f"{i} · {c['nombre']} ✅")
                elif c["nombre"]:
                    lineas.append(f"{i} · {c['nombre']} (probable)")
                elif c["candidato"] and c["puntaje_candidato"] > 0.2:
                    lineas.append(f"{i} · no estoy seguro (¿{c['candidato']}?)")
                else:
                    lineas.append(f"{i} · no lo conozco")
                ultimo["caras"].append(rostros._cod(c["huella"]))
            anotada = rostros.anotar(img, caras, str(tmp / f"q{n_foto}.jpg"), inicio=base + 1)
            texto = "\n".join(lineas) + (
                "\n\nSi alguno está mal o no lo conocí, respondé por ejemplo "
                f"«{base + 1} es Kevyn» y lo aprendo.")
            enviar_foto(tg, chat_id, anotada, texto)
        if ultimo["caras"]:
            # Las huellas de este /quien quedan cifradas para «2 es Kevyn».
            _guardar_ultimo(rostros, ultimo)


def _guardar_ultimo(rostros, ultimo):
    from cryptography.fernet import Fernet
    token = Fernet(rostros._llave()).encrypt(json.dumps(ultimo).encode("utf-8"))
    _escribir(ULTIMO_PATH, {"t": ultimo["t"], "datos": token.decode("ascii")})


def _leer_ultimo(rostros):
    crudo = _leer(ULTIMO_PATH)
    if not crudo.get("datos") or crudo.get("t", 0) < time.time() - ULTIMO_VIGENCIA_SEG:
        return None
    try:
        from cryptography.fernet import Fernet
        return json.loads(Fernet(rostros._llave()).decrypt(crudo["datos"].encode("ascii")))
    except Exception:  # noqa: BLE001
        return None


def _corregir(tg, chat_id, n, nombre):
    import rostros
    ultimo = _leer_ultimo(rostros)
    if not ultimo:
        return False  # no hay un /quien reciente: el texto no era para esto
    if not (1 <= n <= len(ultimo["caras"])):
        tg.reply(chat_id, f"En la última foto que revisé hay {len(ultimo['caras'])} "
                          f"cara(s); la {n} no existe.")
        return True
    # Solo nombres ya registrados: así un «1 es mentira» suelto no crea a
    # nadie. Para alguien nuevo está /participante.
    lista, _ = rostros.listar()
    registrados = {rostros.clave_nombre(p["nombre"]): p["nombre"] for p in lista}
    if rostros.clave_nombre(nombre) not in registrados:
        if len(nombre.split()) > 3:
            return False  # era una frase cualquiera, no una corrección
        tg.reply(chat_id, f"No tengo a nadie registrado como «{nombre}». Si es alguien "
                          f"nuevo, primero /participante {nombre} con sus fotos; "
                          "después me decís de nuevo el número.")
        return True
    vec = rostros._dec(ultimo["caras"][n - 1])
    bonito, total = rostros.agregar_huella(nombre, vec)
    _log(tg, f"Corrección: cara {n} es {bonito} (total {total}).")
    tg.reply(chat_id, f"👍 Anotado: la cara {n} es {bonito}. Ya tiene {total} "
                      "referencia(s). La próxima la reconozco mejor.")
    return True


def _participantes(tg, chat_id):
    import rostros
    lista, aviso = rostros.listar()
    if not lista:
        texto = "Todavía no hay nadie registrado. Usá /participante Nombre y mandame sus fotos."
    else:
        filas = [f"• {p['nombre']} — {p['huellas']} foto(s)"
                 + ("  ⚠️ pocas" if p["huellas"] < 5 else "") for p in lista]
        texto = f"👥 Participantes registrados ({len(lista)}):\n" + "\n".join(filas)
    if aviso:
        texto += f"\n\n⚠️ {aviso}."
    tg.reply(chat_id, texto)


def _olvidar_preguntar(tg, chat_id, nombre):
    import rostros
    clave = rostros.clave_nombre(nombre)
    lista, _ = rostros.listar()
    existe = [p for p in lista if rostros.clave_nombre(p["nombre"]) == clave]
    if not existe:
        tg.reply(chat_id, f"No tengo a nadie registrado como «{nombre}». "
                          "Mirá la lista con /participantes.")
        return
    tg.reply(chat_id, f"¿Borro las {existe[0]['huellas']} foto(s) de {existe[0]['nombre']}? "
                      "No se puede deshacer.",
             {"inline_keyboard": [[
                 {"text": "🗑 Sí, borrar", "callback_data": f"ro|olv|{clave}"[:64]},
                 {"text": "Cancelar", "callback_data": "ro|no"},
             ]]})


def atender_callback(tg, cb, partes):
    """Botones «ro|…» (confirmación de /olvidar)."""
    import rostros
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    if len(partes) >= 3 and partes[1] == "olv":
        borrado = rostros.olvidar(partes[2])
        tg.answer_callback(cb["id"], "Borrado." if borrado else "Ya no estaba.")
        tg.edit_message(chat_id, message_id,
                        f"🗑 Listo: borré las fotos de {borrado}." if borrado
                        else "Ese participante ya no estaba registrado.")
        _log(tg, f"Olvidado: {borrado}")
        return
    tg.answer_callback(cb["id"], "Cancelado.")
    tg.edit_message(chat_id, message_id, "Cancelado, no borré nada.")


# --------------------------------------------------------------------------
# Punto de entrada
# --------------------------------------------------------------------------

def atender(tg, mensajes):
    """Se queda con los mensajes que son de rostros y devuelve el resto."""
    restantes = []
    registros = {}   # nombre -> [file_id, ...]  (en orden de llegada)
    quienes = []     # file_ids para /quien
    grupo_destino = {}  # media_group_id -> ("registrar", nombre) | ("quien", "")
    chat_id = tg.TELEGRAM_CHAT_ID

    # Primero: qué álbumes tienen el comando en alguna de sus fotos.
    for msg in mensajes:
        mgid = msg.get("media_group_id")
        if not mgid:
            continue
        cmd, arg = _comando(msg.get("caption") or "")
        if cmd == "participante" and arg:
            grupo_destino[mgid] = ("registrar", arg)
        elif cmd == "quien":
            grupo_destino[mgid] = ("quien", "")

    for msg in mensajes:
        chat_id = str(msg.get("chat", {}).get("id", "")) or chat_id
        fid = _archivo_imagen(msg)
        texto = (msg.get("text") or "").strip()
        caption = (msg.get("caption") or "").strip()

        if fid:
            cmd, arg = _comando(caption)
            destino = grupo_destino.get(msg.get("media_group_id"))
            if cmd == "participante" and arg:
                destino = ("registrar", arg)
            elif cmd == "participante":
                tg.reply(chat_id, "Falta el nombre: escribí la descripción así: "
                                  "/participante Pascal")
                continue
            elif cmd == "quien":
                destino = ("quien", "")
            elif not destino and not caption:
                p = pendiente()
                if p.get("modo") == "registrar":
                    destino = ("registrar", p["nombre"])
                elif p.get("modo") == "quien":
                    destino = ("quien", "")
            if not destino:
                restantes.append(msg)  # foto normal: va a publicar
                continue
            if destino[0] == "registrar":
                registros.setdefault(destino[1], []).append(fid)
                abrir_pendiente("registrar", destino[1])  # para las que lleguen después
            else:
                quienes.append(fid)
                abrir_pendiente("quien")
            continue

        if not texto:
            restantes.append(msg)
            continue

        cmd, arg = _comando(texto)
        if cmd == "participante":
            if not arg:
                tg.reply(chat_id, "Decime el nombre, por ejemplo: /participante Pascal")
            else:
                abrir_pendiente("registrar", arg)
                tg.reply(chat_id, f"📸 Dale. Mandame ahora fotos de {arg.strip()} donde se le vea "
                                  "bien la cara (ideal 5 a 8: de frente, de perfil, con y sin "
                                  "gorra). Sin descripción. Cuando termines, escribí /listo.")
            continue
        if cmd == "participantes":
            _participantes(tg, chat_id)
            continue
        if cmd == "olvidar":
            if arg:
                _olvidar_preguntar(tg, chat_id, arg)
            else:
                tg.reply(chat_id, "Decime a quién, por ejemplo: /olvidar Pascal")
            continue
        if cmd == "quien":
            abrir_pendiente("quien")
            tg.reply(chat_id, "🔍 Mandame la foto (sin descripción) y te digo quién es quién.")
            continue
        if cmd == "listo":
            p = pendiente()
            cerrar_pendiente()
            if p.get("modo") == "registrar":
                tg.reply(chat_id, f"👌 Listo con {p['nombre']}. Para ver a todos: /participantes. "
                                  "Para probar: /quien y una foto.")
            else:
                tg.reply(chat_id, "👌 Listo.")
            continue

        m = _RE_CORRIGE.match(texto)
        if m:
            try:
                if _corregir(tg, chat_id, int(m.group(1)), m.group(2)):
                    continue
            except Exception as e:  # noqa: BLE001
                _log(tg, f"ERROR en corrección: {e}")
                tg.reply(chat_id, f"❌ No pude anotar la corrección: {e}")
                continue
        restantes.append(msg)

    for nombre, fids in registros.items():
        _registrar(tg, chat_id, nombre, fids)
    if quienes:
        _quien(tg, chat_id, quienes)
    return restantes
