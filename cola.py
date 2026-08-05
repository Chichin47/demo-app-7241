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

Un solo escritor por archivo: así el bot y el panel nunca se pisan, aunque
corran uno detrás del otro en el mismo ciclo.
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
