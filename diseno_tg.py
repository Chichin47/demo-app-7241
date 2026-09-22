#!/usr/bin/env python3
"""Diseños por Telegram: mandás una descripción y 2 a 5 fotos (en un álbum) y
el bot te devuelve la imagen armada en uno de los 5 estilos, con la
descripción propuesta y botones:

  🔁 Rehacer         -> otra versión del mismo estilo (otros textos y colores).
  1a · 2a · 3a ...   -> la misma nota en otro estilo.
  📄 Archivo HD      -> la imagen sin la compresión de Telegram.
  ✖️ Cerrar          -> termina con ese diseño.

Para pedir un cambio puntual, se RESPONDE a la imagen con lo que se quiera
(«más dramático», «la foto 3 grande», «cambiá la descripción: ...») y sale
una versión nueva con eso en cuenta. También vale escribir «rehacer: ...».

Por ahora NADA se publica en ninguna página: todo vuelve a este chat hasta
que el diseño esté pulido. Las fotos sueltas (una sola) siguen yendo al
flujo de publicación de siempre.
"""
import json
import re
import tempfile
import time
import unicodedata
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ESTADO_PATH = BASE_DIR / "state" / "disenos.json"
ESPERA_ALBUM_SEG = 4          # para no cortar un álbum que todavía está llegando
VIDA_SEG = 48 * 3600
MAX_FOTOS = 5
MAX_POR_PASADA = 3
ETIQUETAS = {"1a": "1a Detalle", "2a": "2a Duelo", "3a": "3a Clásico",
             "4a": "4a Mosaico", "5a": "5a Círculos"}


def _log(tg, msg):
    tg.log(f"[diseño] {msg}")


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------

def cargar():
    try:
        d = json.loads(ESTADO_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("albumes", {})
    d.setdefault("jobs", {})
    return d


def guardar(d):
    corte = time.time() - VIDA_SEG
    d["jobs"] = {k: j for k, j in d["jobs"].items() if j.get("creado", 0) > corte}
    if len(d["jobs"]) > 40:
        for k in sorted(d["jobs"], key=lambda k: d["jobs"][k].get("creado", 0))[:-40]:
            d["jobs"].pop(k, None)
    d["albumes"] = {k: a for k, a in d["albumes"].items() if a.get("ultimo", 0) > corte}
    ESTADO_PATH.parent.mkdir(parents=True, exist_ok=True)
    ESTADO_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _norm(t):
    t = unicodedata.normalize("NFD", (t or "").strip().lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _archivo_imagen(msg):
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"]
    doc = msg.get("document") or {}
    if (doc.get("mime_type") or "").startswith("image/"):
        return doc.get("file_id")
    return None


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _enviar_foto(tg, chat_id, ruta, caption, markup=None):
    import rostros_tg
    return rostros_tg.enviar_foto(tg, chat_id, ruta, caption, markup)


def _enviar_documento(tg, chat_id, ruta, caption):
    import requests
    try:
        with open(ruta, "rb") as fh:
            r = requests.post(
                f"https://api.telegram.org/bot{tg.TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (Path(ruta).name, fh, "image/png")}, timeout=120)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        _log(tg, f"No se pudo mandar el archivo: {e}")
        tg.reply(chat_id, f"❌ No pude mandar el archivo: {e}")
        return {}


def _botones(key, estilo, posibles):
    otros = [{"text": ETIQUETAS[e], "callback_data": f"dz|es|{key}|{e}"}
             for e in posibles if e != estilo]
    filas = [[{"text": "🔁 Rehacer", "callback_data": f"dz|rh|{key}"},
              {"text": "📄 Archivo HD", "callback_data": f"dz|hd|{key}"}]]
    if otros:
        filas.append(otros[:3])
        if otros[3:]:
            filas.append(otros[3:])
    filas.append([{"text": "✖️ Cerrar", "callback_data": f"dz|x|{key}"}])
    return {"inline_keyboard": filas}


def _bajar(tg, file_ids, carpeta):
    rutas = []
    for i, fid in enumerate(file_ids, 1):
        destino = carpeta / f"foto{i}.jpg"
        tg.download_telegram_photo(fid, destino)
        rutas.append(destino)
    return rutas


# --------------------------------------------------------------------------
# Generar
# --------------------------------------------------------------------------

def _edicion(tg, job):
    """Descripción y frases del post (el pedido de siempre, una vez por nota)."""
    if job.get("edit"):
        return job["edit"]
    bot = tg.bot
    try:
        e = bot.ask_claude(job["descripcion"], len(job["fotos"]), manual=True)
        edit = {"caption": bot.quitar_etiqueta((e.get("caption") or "").strip()) or job["descripcion"],
                "lines": [{"text": l.get("text")} for l in e.get("lines") or [] if l.get("text")]}
    except Exception as e:  # noqa: BLE001
        _log(tg, f"No se pudo pedir la descripción ({e}); uso el texto tal cual.")
        edit = {"caption": job["descripcion"], "lines": []}
    job["edit"] = edit
    return edit


def generar(tg, job, estilo=None, instruccion=None, rehacer=False):
    import diseno
    import estilos
    chat_id = job["chat_id"]
    if instruccion:
        job.setdefault("instrucciones", []).append(instruccion.strip())
    aviso = tg.reply(chat_id, "⏳ Armando el diseño" + (f" en estilo {estilo}" if estilo else "")
                     + "… (tarda alrededor de un minuto)")
    aviso_id = (aviso or {}).get("result", {}).get("message_id")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rutas = _bajar(tg, job["fotos"], tmp)
            edit = _edicion(tg, job)
            planes = job.setdefault("planes", [])
            anteriores = [p.get("resumen") for p in planes if p.get("resumen")]
            if estilo is None and planes and (rehacer or instruccion):
                # Se mantiene el estilo, salvo que la instrucción hable de estilos.
                m = re.search(r"\b([1-5])\s*a\b", _norm(instruccion or ""))
                if m:
                    estilo = f"{m.group(1)}a"
                elif not re.search(r"estilo|duelo|mosaico|circul|clasico|detalle|formato|diseno",
                                   _norm(instruccion or "")):
                    estilo = planes[-1]["plan"]["estilo"]
            salida = tmp / "diseno.png"
            plan, datos = diseno.disenar(
                job["descripcion"], rutas, salida, edit=edit, estilo_pedido=estilo,
                instrucciones=job.get("instrucciones", [])[-4:],
                anteriores=anteriores if (rehacer or instruccion or estilo) else None,
                anterior_plan=planes[-1]["plan"] if planes else None,
                pedir_claude=getattr(tg, "pedir_claude_diseno", None))
            if datos.get("caption"):
                edit["caption"] = str(datos["caption"]).strip()
            version = len(planes) + 1
            posibles = diseno.estilos_posibles(plan["fotos_info"])
            nombre = estilos.NOMBRES.get(plan["estilo"], plan["estilo"])
            cabeza = (f"🎨 Versión {version} · {nombre} ({plan['estilo']})\n\n")
            pie = "\n\n💬 Respondé a esta imagen con lo que quieras cambiar."
            cuerpo = edit["caption"]
            caption = cabeza + cuerpo + pie
            aparte = None
            if len(caption) > 1024:
                caption = cabeza + "📝 La descripción va en el mensaje de abajo." + pie
                aparte = cuerpo
            res = _enviar_foto(tg, chat_id, salida, caption,
                               _botones(job["key"], plan["estilo"], posibles))
            if aparte:
                tg.reply(chat_id, aparte)
            pid = (res or {}).get("result", {}).get("message_id")
            resumen = {k: datos.get(k) for k in ("estilo", "titular", "subtitulo", "frases", "emojis")
                       if datos.get(k)}
            planes.append({"plan": plan, "preview": pid, "resumen": resumen})
            job["planes"] = planes[-6:]
            if pid:
                job.setdefault("previews", []).append(pid)
                job["previews"] = job["previews"][-12:]
            job["estado"] = "abierto"
            _log(tg, f"{job['key']}: versión {version} en {plan['estilo']} enviada.")
    except Exception as e:  # noqa: BLE001
        _log(tg, f"ERROR armando el diseño de {job['key']}: {e}")
        tg.reply(chat_id, f"❌ No pude armar el diseño: {e}")
    finally:
        if aviso_id:
            tg.borrar_mensaje(chat_id, aviso_id)


def archivo_hd(tg, job, preview_id=None):
    import estilos
    elegido = None
    for p in job.get("planes", []):
        if preview_id and p.get("preview") == preview_id:
            elegido = p
    elegido = elegido or (job.get("planes") or [None])[-1]
    if not elegido:
        tg.reply(job["chat_id"], "Ese diseño ya no está disponible.")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rutas = _bajar(tg, job["fotos"], tmp)
        salida = tmp / f"universo_reality_{job['key']}_{elegido['plan']['estilo']}.png"
        estilos.render(json.loads(json.dumps(elegido["plan"])), rutas, salida)
        _enviar_documento(tg, job["chat_id"], salida, "📄 En alta, sin la compresión de Telegram.")


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def atender(tg, mensajes, ahora=None):
    """Se queda con los álbumes y las respuestas a diseños; devuelve el resto."""
    ahora = ahora or time.time()
    d = cargar()
    restantes = []
    por_preview = {pid: k for k, j in d["jobs"].items() for pid in j.get("previews", [])}
    por_pedido = {j["pedido_texto"]: k for k, j in d["jobs"].items() if j.get("pedido_texto")}
    trabajos = []   # (key, estilo, instruccion, rehacer)
    nuevos_albumes = set()

    # 1. Fotos de álbumes: se juntan (pueden llegar repartidas en dos lecturas).
    textos = []
    for msg in mensajes:
        fid = _archivo_imagen(msg)
        mgid = msg.get("media_group_id")
        if fid and mgid:
            a = d["albumes"].setdefault(mgid, {"fotos": [], "caption": "", "ids": [],
                                               "chat_id": str(msg.get("chat", {}).get("id", "")),
                                               "primero": msg.get("message_id")})
            if fid not in a["fotos"]:
                a["fotos"].append(fid)
                a["ids"].append(msg.get("message_id"))
            if (msg.get("caption") or "").strip() and not a["caption"]:
                a["caption"] = msg["caption"].strip()
            a["ultimo"] = max(a.get("ultimo", 0), msg.get("date") or ahora)
            nuevos_albumes.add(mgid)
            continue
        if (msg.get("text") or "").strip() and not fid:
            textos.append(msg)
            continue
        restantes.append(msg)

    # 2. Textos: respuestas a un diseño, la descripción que faltaba, o el
    #    texto que vino suelto al lado de un álbum.
    for msg in textos:
        texto = msg["text"].strip()
        resp = (msg.get("reply_to_message") or {}).get("message_id")
        if resp in por_preview:
            trabajos.append((por_preview[resp], None, texto, False))
            continue
        if resp in por_pedido:
            job = d["jobs"][por_pedido[resp]]
            job["descripcion"] = texto
            job.pop("pedido_texto", None)
            trabajos.append((job["key"], None, None, False))
            continue
        n = _norm(texto)
        if n.startswith("rehacer"):
            abiertos = [j for j in d["jobs"].values() if j.get("estado") == "abierto"]
            if abiertos:
                ultimo = max(abiertos, key=lambda j: j.get("creado", 0))
                instr = texto[len("rehacer"):].lstrip(" :,-")
                trabajos.append((ultimo["key"], None, instr or None, not instr))
                continue
        pegado = None
        for mgid in ([] if texto.startswith("/") else nuevos_albumes):
            a = d["albumes"].get(mgid)
            if a and not a["caption"] and any(abs((msg.get("message_id") or 0) - i) <= 3 for i in a["ids"]):
                pegado = a
                break
        if pegado is not None:
            pegado["caption"] = texto
            continue
        restantes.append(msg)

    # 3. Álbumes completos -> diseño nuevo.
    for mgid in list(d["albumes"]):
        a = d["albumes"][mgid]
        if a.get("ultimo", 0) > ahora - ESPERA_ALBUM_SEG and mgid in nuevos_albumes and not a.get("visto"):
            a["visto"] = True  # le damos una vuelta más por si faltan fotos
            continue
        d["albumes"].pop(mgid)
        key = f"d{a['primero']}"
        fotos = a["fotos"][:MAX_FOTOS]
        job = {"key": key, "chat_id": a["chat_id"] or tg.TELEGRAM_CHAT_ID, "fotos": fotos,
               "descripcion": a["caption"], "creado": time.time(), "estado": "nuevo"}
        d["jobs"][key] = job
        if len(a["fotos"]) > MAX_FOTOS:
            tg.reply(job["chat_id"], f"Llegaron {len(a['fotos'])} fotos; uso las primeras {MAX_FOTOS}.")
        if not job["descripcion"]:
            r = tg.reply(job["chat_id"], f"📸 Recibí {len(fotos)} fotos sin descripción. "
                                         "Respondé a ESTE mensaje con la descripción del post y armo el diseño.")
            job["pedido_texto"] = (r or {}).get("result", {}).get("message_id")
            job["estado"] = "sin_texto"
            continue
        trabajos.append((key, None, None, False))
    # 4. Generar (lo más lento: va al final). Como mucho MAX_POR_PASADA por
    #    pasada, para no pasarnos del tiempo que el runner le da al chat; lo
    #    que sobra queda anotado y sale en la pasada siguiente.
    cola = [list(t) for t in d.get("pendientes", [])] + [list(t) for t in trabajos]
    d["pendientes"] = cola[MAX_POR_PASADA:]
    guardar(d)
    for key, estilo, instr, rehacer in cola[:MAX_POR_PASADA]:
        job = d["jobs"].get(key)
        if not job:
            continue
        generar(tg, job, estilo=estilo, instruccion=instr, rehacer=rehacer)
        guardar(d)
    return restantes


def atender_callback(tg, cb, partes):
    """Botones «dz|accion|key[|estilo]»."""
    d = cargar()
    accion = partes[1] if len(partes) > 1 else ""
    key = partes[2] if len(partes) > 2 else ""
    job = d["jobs"].get(key)
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    message_id = cb.get("message", {}).get("message_id")
    if not job:
        tg.answer_callback(cb["id"], "Ese diseño ya no está.")
        return
    if accion == "x":
        job["estado"] = "cerrado"
        tg.answer_callback(cb["id"], "Cerrado.")
        try:
            tg.api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": []})
        except Exception:  # noqa: BLE001
            pass
        tg.reply(chat_id, "✅ Listo, cerré ese diseño.")
    elif accion == "hd":
        tg.answer_callback(cb["id"], "Te mando el archivo…")
        archivo_hd(tg, job, message_id)
    elif accion == "rh":
        tg.answer_callback(cb["id"], "Rehaciendo…")
        generar(tg, job, rehacer=True)
    elif accion == "es" and len(partes) > 3:
        tg.answer_callback(cb["id"], f"Armando en {partes[3]}…")
        generar(tg, job, estilo=partes[3])
    guardar(d)
