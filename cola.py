#!/usr/bin/env python3
"""Cola visible de la página 1: la lista de lo que está por publicarse.

Hasta ahora la "cola" no existía como tal. El bot miraba la página 1 en cada
barrido, descartaba lo que ya había publicado y agarraba el más antiguo que
quedara. Los posts en espera existían, pero solo como consecuencia de la regla
de un post cada X minutos: no había ninguna lista que mirar.

Este módulo es esa lista, y el mando a distancia para tocarla. Son tres
archivos, a propósito separados:

  state/cola_snapshot.json   Lo ESCRIBE el bot en cada barrido: la foto del
                             momento de lo que está pendiente en la página 1.
                             El panel solo lo lee. Leerlo no cuesta nada: es
                             lo que el bot ya había traído de Facebook.

  state/cola_control.json    Lo ESCRIBE el panel de Telegram cuando tocas un
                             botón: qué está pausado, qué quieres que salga
                             primero y qué descartaste. El bot solo lo lee y
                             lo obedece.

  state/cola_panel.json      Los mensajes del último panel dibujado en el
                             chat, para poder borrarlos al redibujar y que no
                             se llene la conversación de paneles viejos.

  state/rehacer.json         Los encargos de "hacé el video de este post que ya
                             salió como foto". Este es el único archivo que
                             tocan los dos: el panel apunta el encargo y el bot
                             anota cómo le fue. Se pueden pisar porque nunca
                             corren a la vez —runner.py los llama uno detrás
                             del otro— y porque cada uno lee, cambia lo suyo y
                             vuelve a escribir el archivo entero.

Un solo escritor por archivo (salvo el de rehacer, recién explicado): así el
bot y el panel nunca se pisan, aunque corran uno detrás del otro en el mismo
ciclo.
"""
import json
import time
import hashlib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = BASE_DIR / "state" / "cola_snapshot.json"
CONTROL_PATH = BASE_DIR / "state" / "cola_control.json"
PANEL_PATH = BASE_DIR / "state" / "cola_panel.json"
REINICIO_PATH = BASE_DIR / "state" / "reinicio.json"
REHACER_PATH = BASE_DIR / "state" / "rehacer.json"

# Cuántos pendientes se guardan en la foto de la cola. Más que esto no aporta:
# a ese ritmo son horas de trabajo por delante, y el archivo de estado se sube
# al repositorio en cada ciclo.
MAX_ITEMS = 12

# Máximo de caracteres del texto original que se guarda por post. Alcanza de
# sobra para reconocerlo en el panel.
MAX_TEXTO = 400

# Pasado este tiempo sin actualizarse, la foto de la cola se considera vieja
# (el bot no está barriendo).
FRESCA_MINUTOS = 12

# Las listas del archivo de control.
# "video" y "foto" son el formato pedido a mano desde el panel: si un post no
# está en ninguna de las dos, el bot decide solo cómo sale.
LISTAS = ("pausados", "prioridad", "eliminados", "video", "foto")


def clave(post_id):
    """Identificador corto y estable para los botones.

    Telegram limita el `callback_data` a 64 bytes y los identificadores de
    Facebook son largos, así que en los botones viaja este resumen de 10
    caracteres y el panel lo traduce al identificador real usando la foto de
    la cola.
    """
    return hashlib.sha1(str(post_id).encode("utf-8")).hexdigest()[:10]


def _leer(ruta, por_defecto):
    try:
        if ruta.exists():
            return json.loads(ruta.read_text(encoding="utf-8"))
    except Exception:
        pass
    return por_defecto


def _escribir(ruta, dato):
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(dato, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Foto de la cola (la escribe el bot, la lee el panel)
# --------------------------------------------------------------------------

def guardar_snapshot(items, extra=None):
    dato = {"actualizado": time.time(), "items": items[:MAX_ITEMS]}
    if extra:
        dato.update(extra)
    return _escribir(SNAPSHOT_PATH, dato)


def leer_snapshot():
    return _leer(SNAPSHOT_PATH, {})


def edad_snapshot(snap=None):
    """Hace cuántos segundos se actualizó la foto de la cola. None si no hay."""
    snap = snap if snap is not None else leer_snapshot()
    ts = snap.get("actualizado")
    if not ts:
        return None
    return max(0.0, time.time() - float(ts))


def item_a_texto(item, limite=MAX_TEXTO):
    return (item.get("texto") or "").strip()[:limite]


# --------------------------------------------------------------------------
# Control manual (lo escribe el panel, lo lee el bot)
# --------------------------------------------------------------------------

def leer_control():
    ctrl = _leer(CONTROL_PATH, {})
    for nombre in LISTAS:
        valor = ctrl.get(nombre)
        ctrl[nombre] = list(valor) if isinstance(valor, list) else []
    return ctrl


def guardar_control(ctrl):
    limpio = {nombre: sorted(set(ctrl.get(nombre) or [])) for nombre in LISTAS}
    return _escribir(CONTROL_PATH, limpio)


def marcar(post_id, lista, poner=True):
    """Pone o saca un post de una de las listas de control."""
    if lista not in LISTAS:
        return False
    ctrl = leer_control()
    actual = set(ctrl.get(lista) or [])
    if poner:
        actual.add(str(post_id))
    else:
        actual.discard(str(post_id))
    ctrl[lista] = sorted(actual)
    # "Publicar ahora" y "pausar" son incompatibles: al pedir uno se suelta el
    # otro, así no queda un post marcado como urgente y congelado a la vez.
    if poner and lista == "prioridad":
        ctrl["pausados"] = sorted(set(ctrl.get("pausados") or []) - {str(post_id)})
    if poner and lista == "pausados":
        ctrl["prioridad"] = sorted(set(ctrl.get("prioridad") or []) - {str(post_id)})
    # El formato es uno solo: pedir video suelta la marca de foto y al revés.
    if poner and lista in ("video", "foto"):
        otro = "foto" if lista == "video" else "video"
        ctrl[otro] = sorted(set(ctrl.get(otro) or []) - {str(post_id)})
    return guardar_control(ctrl)


def formato_pedido(post_id, ctrl=None):
    """Devuelve "reel", "foto" o None según lo que se haya pedido en el panel."""
    ctrl = ctrl if ctrl is not None else leer_control()
    pid = str(post_id)
    if pid in set(ctrl.get("video") or []):
        return "reel"
    if pid in set(ctrl.get("foto") or []):
        return "foto"
    return None


def limpiar_control(vigentes):
    """Borra del control los posts que ya no están pendientes.

    Sin esto el archivo crecería para siempre con identificadores de posts que
    ya se publicaron o que Facebook ya no devuelve.
    """
    vigentes = {str(x) for x in vigentes}
    ctrl = leer_control()
    cambio = False
    for nombre in ("pausados", "prioridad", "video", "foto"):
        antes = set(ctrl.get(nombre) or [])
        despues = antes & vigentes
        if antes != despues:
            ctrl[nombre] = sorted(despues)
            cambio = True
    # Los eliminados se sueltan recién cuando ya quedaron marcados como
    # procesados; si se soltaran antes, volverían a aparecer en la cola.
    if cambio:
        guardar_control(ctrl)
    return ctrl


def soltar_eliminados(ids):
    """Saca de la lista de eliminados los que ya quedaron marcados para siempre."""
    ids = {str(x) for x in ids}
    if not ids:
        return
    ctrl = leer_control()
    quedan = set(ctrl.get("eliminados") or []) - ids
    ctrl["eliminados"] = sorted(quedan)
    guardar_control(ctrl)


# --------------------------------------------------------------------------
# Encargos de video sobre publicaciones que YA salieron
# --------------------------------------------------------------------------
#
# El formato (foto o video) se elige antes de publicar, desde el panel de la
# cola. Pero a veces el post ya salió como foto y recién ahí se ve que daba
# para video. Esto es para eso: pedir el video de algo ya publicado.
#
# El video nuevo se arma desde el post ORIGINAL de la página 1 (misma foto,
# mismo texto), no desde lo que se publicó. Y se publica aparte: la foto que ya
# está en la página 2 no se toca ni se borra, porque borrar es cosa tuya.

# Cuántos encargos se guardan. Pasado esto se van cayendo los más viejos ya
# atendidos: el archivo de estado viaja al repositorio en cada ciclo.
MAX_REHACER = 20


def leer_rehacer():
    dato = _leer(REHACER_PATH, {})
    pedidos = dato.get("pedidos")
    return list(pedidos) if isinstance(pedidos, list) else []


def _guardar_rehacer(pedidos):
    # Se conservan todos los pendientes y, de los ya atendidos, solo los
    # últimos: son los que el panel muestra como "ya está hecho".
    pendientes = [p for p in pedidos if p.get("estado") == "pendiente"]
    resto = [p for p in pedidos if p.get("estado") != "pendiente"]
    resto = resto[-max(0, MAX_REHACER - len(pendientes)):] if resto else []
    return _escribir(REHACER_PATH, {"pedidos": pendientes + resto})


def pedir_video(source_post_id, publicado="", texto="", formato="reel",
                destino="ambos"):
    """Apunta el encargo. Si ya estaba pedido y sin atender, no duplica.

    formato dice CÓMO se rehace: "reel" (el de siempre) o "foto". El de foto
    existe para cuando una publicación salió mal o quedó invisible y la
    borraste: con esto se vuelve a publicar tal como habría salido, leyendo el
    original de la página 1 tal como esté en ese momento.

    destino dice A DÓNDE va: "ambos" (lo normal), "fb" o "ig". Sirve para las
    limpiezas a medias: si la copia de Instagram quedó bien y solo falta la de
    Facebook, no tiene sentido duplicar la de Instagram, y al revés igual.
    """
    pid = str(source_post_id)
    pedidos = leer_rehacer()
    for p in pedidos:
        if str(p.get("source")) == pid and p.get("estado") == "pendiente":
            return False, "ya"
    pedidos = [p for p in pedidos if str(p.get("source")) != pid]
    pedidos.append({
        "source": pid,
        "publicado": str(publicado or ""),
        "texto": (texto or "")[:MAX_TEXTO],
        # "reel" y "foto" republican; "chat" solo reenvía al chat el video ya
        # publicado (lo baja de Facebook y lo manda por Telegram, sin tokens).
        "formato": formato if formato in ("foto", "chat") else "reel",
        "destino": destino if destino in ("fb", "ig") else "ambos",
        "pedido": time.time(),
        "estado": "pendiente",
        "detalle": "",
        "resultado": "",
    })
    return _guardar_rehacer(pedidos), "nuevo"


def cancelar_video(source_post_id):
    """Saca un encargo que todavía no se atendió."""
    pid = str(source_post_id)
    pedidos = leer_rehacer()
    quedan = [p for p in pedidos
              if not (str(p.get("source")) == pid and p.get("estado") == "pendiente")]
    if len(quedan) == len(pedidos):
        return False
    return _guardar_rehacer(quedan)


def rehacer_pendientes():
    """Los encargos sin atender, del más viejo al más nuevo."""
    pendientes = [p for p in leer_rehacer() if p.get("estado") == "pendiente"]
    pendientes.sort(key=lambda p: p.get("pedido") or 0)
    return pendientes


def estado_video(source_post_id):
    """Cómo quedó el encargo de este post: 'pendiente', 'hecho', 'error' o None."""
    pid = str(source_post_id)
    for p in reversed(leer_rehacer()):
        if str(p.get("source")) == pid:
            return p.get("estado")
    return None


def cerrar_video(source_post_id, estado, detalle="", resultado=""):
    """Lo anota el bot cuando termina de atender un encargo."""
    pid = str(source_post_id)
    pedidos = leer_rehacer()
    tocado = False
    for p in pedidos:
        if str(p.get("source")) == pid and p.get("estado") == "pendiente":
            p["estado"] = estado
            p["detalle"] = str(detalle)[:300]
            p["resultado"] = str(resultado or "")
            p["cerrado"] = time.time()
            tocado = True
    if not tocado:
        return False
    return _guardar_rehacer(pedidos)


# --------------------------------------------------------------------------
# Mensajes del panel dibujado en el chat
# --------------------------------------------------------------------------

def leer_panel():
    return _leer(PANEL_PATH, {})


def guardar_panel(chat_id, message_ids):
    return _escribir(PANEL_PATH, {
        "chat_id": str(chat_id),
        "mensajes": list(message_ids),
        "ts": time.time(),
    })


# --------------------------------------------------------------------------
# Reinicio pedido desde el chat
# --------------------------------------------------------------------------

def pedir_reinicio(motivo="telegram"):
    """Deja la orden de reinicio. La recoge runner.py al terminar el ciclo."""
    return _escribir(REINICIO_PATH, {"pedido": time.time(), "motivo": motivo})


def hay_reinicio():
    dato = _leer(REINICIO_PATH, {})
    return bool(dato.get("pedido"))


def limpiar_reinicio():
    # Se deja el archivo en cero en vez de borrarlo: el estado se guarda
    # uniendo lo nuevo con lo que ya había en el repositorio, así que un
    # archivo borrado revive en el relevo y el bot se reiniciaría en bucle.
    # Escrito en cero, la orden queda anulada de verdad.
    return _escribir(REINICIO_PATH, {"pedido": 0, "atendido": time.time()})
