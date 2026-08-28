#!/usr/bin/env python3
"""
Runner — mantiene el bot corriendo solo dentro de un job largo.

En vez de depender del cron (que no respeta el horario pedido: se pidió cada
3 min y corría cada ~3 horas), aquí el bucle es nuestro: un solo job vive
hasta MAX_RUNTIME_SECONDS barriendo cada SWEEP_SECONDS, y al terminar el
siguiente job ya encolado arranca de inmediato.

Cada ciclo hace exactamente lo mismo que hacía el workflow:

1. `telegram_listener.py` — atiende lo que mandaste a mano (foto + descripción,
   menú de horarios, cola de programados). Va primero porque lo tuyo tiene
   prioridad sobre el barrido automático.
2. `poll_and_publish.py` — barre la página 1 y publica en la página 2.

Cada script corre como subproceso aparte, igual que en Actions: si uno se
cuelga o revienta, el bucle sigue vivo y lo reintenta en el ciclo siguiente.

Además, cada cierto tiempo corre `selfcheck.py` y te manda el autochequeo por
Telegram, para que sepas que el servidor sigue despierto sin tener que entrar
a mirarlo.

Configuración (variables de entorno, todas opcionales menos los secretos):

  SWEEP_SECONDS      segundos entre barridos (por defecto 180 = 3 min)
  ATENCION_SECONDS   cada cuánto se mira el chat MIENTRAS se espera el próximo
                     barrido (por defecto 25). Es lo que hace que los botones
                     respondan en seguida sin tener que barrer la página 1 más
                     seguido. En 0 se apaga.
  JITTER_SECONDS     variación aleatoria que se suma/resta al intervalo
                     (por defecto 20). Evita caer siempre en el segundo
                     exacto, que es un patrón muy de robot.
  STEP_TIMEOUT       segundos máximos por script antes de cortarlo (600)
  SELFCHECK_HOURS    cada cuántas horas mandar el autochequeo (12; 0 lo apaga)
  TZ_OFFSET_HOURS    huso horario para los logs (-5 = Lima)
  MAX_RUNTIME_SECONDS  cuánto vive el proceso antes de cerrarse solo para que
                     lo releve el siguiente turno (0 = sin límite)
  QUIET_LIFECYCLE    1 = no avisa por Telegram cada arranque/cierre (los
                     relevos son cada hora; avisar cada vez sería spam)
"""
import os
import sys
import json
import time
import random
import signal
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import cola

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
RELOJ_SELFCHECK = BASE_DIR / "state" / "selfcheck_clock.json"
GUARDAR = BASE_DIR / "tools" / "guardar.sh"

REQUERIDAS = (
    "PAGE_ID_MAIN",
    "PAGE_TOKEN_MAIN",
    "PAGE_ID_BACKUP",
    "PAGE_TOKEN_BACKUP",
    # La llave de la suscripción: es con la que se escribe el texto de los
    # posts. Antes acá iba ANTHROPIC_API_KEY; el bot ya no usa la API.
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def env_num(nombre, por_defecto, tipo=float):
    """Lee una variable numérica tolerando que venga vacía o mal escrita."""
    bruto = (os.environ.get(nombre) or "").strip()
    if not bruto:
        return tipo(por_defecto)
    try:
        return tipo(bruto)
    except ValueError:
        print(f"[runner] Valor inválido en {nombre}={bruto!r}; se usa {por_defecto}.", flush=True)
        return tipo(por_defecto)


SWEEP_SECONDS = max(30.0, env_num("SWEEP_SECONDS", 180, float))
JITTER_SECONDS = max(0.0, env_num("JITTER_SECONDS", 20, float))
# Cada cuánto se pasa por el chat MIENTRAS se espera el próximo barrido. Es lo
# que hace que tocar un botón se sienta inmediato en vez de tardar lo que falte
# para el barrido. No tiene nada que ver con la página 1: eso sigue mirándose
# cada SWEEP_SECONDS. En 0 se apaga y el chat vuelve a atenderse una vez por
# ciclo, como antes.
ATENCION_SECONDS = max(0.0, env_num("ATENCION_SECONDS", 25, float))
STEP_TIMEOUT = max(60.0, env_num("STEP_TIMEOUT", 600, float))
SELFCHECK_HOURS = max(0.0, env_num("SELFCHECK_HOURS", 12, float))
TZ_OFFSET_HOURS = env_num("TZ_OFFSET_HOURS", -5, float)
MAX_RUNTIME_SECONDS = max(0.0, env_num("MAX_RUNTIME_SECONDS", 0, float))
QUIET_LIFECYCLE = (os.environ.get("QUIET_LIFECYCLE") or "").strip() in ("1", "si", "sí", "true")

LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
TZ_LABEL = "UTC" + (f"{TZ_OFFSET_HOURS:+.0f}" if TZ_OFFSET_HOURS else "")

TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

# Tras esta cantidad de ciclos fallidos seguidos avisa por Telegram (una sola
# vez por racha, para no llenarte el chat si el problema dura horas).
FALLOS_PARA_AVISAR = 5

_parar = False


def log(msg):
    ahora = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[runner {ahora}] {msg}", flush=True)


def _senal(signum, _frame):
    global _parar
    _parar = True
    log(f"Señal {signum} recibida; cierro al terminar el ciclo actual.")


def avisar_telegram(texto):
    """Manda un aviso al chat. Nunca revienta el bucle si Telegram falla."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": texto[:4096]},
            timeout=30,
        )
    except Exception as e:
        log(f"No se pudo avisar por Telegram: {e}")


def leer_reloj_selfcheck():
    """Cuándo toca el próximo autochequeo, en tiempo real (no monotónico).

    El proceso se releva cada hora, así que un contador en memoria nunca
    llegaría a las 12 h. Se guarda en disco para que sobreviva al relevo.
    """
    try:
        dato = json.loads(RELOJ_SELFCHECK.read_text(encoding="utf-8"))
        return float(dato.get("proximo", 0))
    except Exception:
        return 0.0


def guardar_reloj_selfcheck(cuando):
    try:
        RELOJ_SELFCHECK.parent.mkdir(parents=True, exist_ok=True)
        RELOJ_SELFCHECK.write_text(
            json.dumps({"proximo": cuando}), encoding="utf-8"
        )
    except Exception as e:
        log(f"No se pudo guardar el reloj del autochequeo: {e}")


def correr(script, silencioso=False):
    """Corre un script del proyecto como subproceso. Devuelve True si salió bien.

    Con silencioso=True no se anota el "OK en tal cosa": son las pasadas cortas
    por el chat, que ocurren muchas veces por ciclo y llenarían el log de ruido.
    Los errores sí se anotan siempre.
    """
    ruta = BASE_DIR / script
    if not ruta.exists():
        log(f"{script}: no existe en {BASE_DIR}; me lo salto.")
        return False
    inicio = time.monotonic()
    try:
        proc = subprocess.run(
            [PYTHON, str(ruta)],
            cwd=str(BASE_DIR),
            timeout=STEP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log(f"{script}: pasó de {STEP_TIMEOUT:.0f}s sin terminar; lo corté.")
        return False
    except Exception as e:
        log(f"{script}: no se pudo ejecutar ({e}).")
        return False
    dur = time.monotonic() - inicio
    if proc.returncode != 0:
        log(f"{script}: terminó con error (código {proc.returncode}) en {dur:.1f}s.")
        return False
    if not silencioso:
        log(f"{script}: OK en {dur:.1f}s.")
    return True


def guardar_estado():
    """Sube al repositorio lo que se publicó, apenas termina el ciclo.

    Antes el registro se guardaba una sola vez, al final del turno. Si el
    turno se cortaba de golpe, se perdía la hora entera de registro y el
    turno siguiente volvía a publicar lo mismo. Guardando cada ciclo, en el
    peor caso se pierden un par de minutos.
    """
    if not (os.environ.get("GITHUB_ACTIONS") or "").strip():
        return  # fuera de Actions no hay a dónde guardar
    if not GUARDAR.exists():
        return
    try:
        proc = subprocess.run(
            ["bash", str(GUARDAR)],
            cwd=str(BASE_DIR),
            timeout=180,
            capture_output=True,
            text=True,
        )
    except Exception as e:  # noqa: BLE001
        log(f"No se pudo guardar el registro: {e}")
        return
    salida = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0:
        log(f"El registro no se pudo guardar: {salida[-1] if salida else 'sin detalle'}")
    elif salida and salida[-1] == "Estado guardado.":
        log("Registro guardado.")


def esperar(segundos):
    """Duerme en tramos cortos para poder cortar rápido si llega un SIGTERM."""
    fin = time.monotonic() + segundos
    while not _parar:
        queda = fin - time.monotonic()
        if queda <= 0:
            return
        time.sleep(min(2.0, queda))


def _huella_estado():
    """Foto de cómo están los archivos de estado, para notar si algo cambió."""
    try:
        return {p.name: p.stat().st_mtime_ns
                for p in sorted((BASE_DIR / "state").glob("*.json"))}
    except Exception:
        return {}


def esperar_atendiendo(segundos):
    """Espera hasta el próximo barrido, pero mirando el chat cada tanto.

    El barrido de la página 1 puede seguir siendo cada tres minutos —los posts
    nuevos no aparecen más rápido que eso—, pero el chat no puede esperar tres
    minutos: tocás un botón y querés que pase algo. Antes el listener corría una
    sola vez por ciclo, así que una respuesta tardaba lo que faltara para el
    próximo barrido, y encima había que sumarle lo que durara ese barrido (si
    justo estaba armando un reel, varios minutos más).

    Ahora, mientras se espera, se pasa por el chat cada ATENCION_SECONDS. El
    barrido pesado no se toca; lo único que se repite es el listener, que es
    barato: se levanta, lee lo que llegó y se va.

    Devuelve True si hay que cortar el ciclo (pediste reiniciar o llegó una
    señal de apagado).
    """
    fin = time.monotonic() + segundos
    while not _parar:
        queda = fin - time.monotonic()
        if queda <= 0:
            return False
        # Si lo que falta es menos que un tramo, se duerme y listo: no vale la
        # pena levantar el listener para adelantarse dos segundos al barrido.
        if not ATENCION_SECONDS or queda <= ATENCION_SECONDS + 5:
            esperar(queda)
            return False

        esperar(ATENCION_SECONDS)
        if _parar:
            return True

        antes = _huella_estado()
        correr("telegram_listener.py", silencioso=True)

        # El botón 🔄 Reiniciar deja la orden escrita; hay que recogerla acá
        # también, porque si no se quedaría esperando al próximo barrido, que es
        # justo lo que se está tratando de no hacer.
        if cola.hay_reinicio():
            return True

        # Solo se sube el registro si el listener movió algo. Sin esto habría un
        # empuje al repositorio cada tanto sin nada adentro; con esto, cuando
        # atendés algo por chat queda guardado enseguida y no se pierde si el
        # turno se corta.
        if _huella_estado() != antes:
            guardar_estado()
    return True


def faltan_secretos():
    return [n for n in REQUERIDAS if not (os.environ.get(n) or "").strip()]


def main():
    signal.signal(signal.SIGTERM, _senal)
    signal.signal(signal.SIGINT, _senal)

    faltan = faltan_secretos()
    if faltan:
        log("No puedo arrancar: faltan estos datos en la configuración: "
            + ", ".join(faltan))
        return 1

    log(f"Arrancando. Barrido cada {SWEEP_SECONDS/60:.1f} min "
        f"(±{JITTER_SECONDS:.0f}s), hora local {TZ_LABEL}.")
    if not QUIET_LIFECYCLE:
        avisar_telegram(
            "🟢 Bot iniciado.\n\n"
            f"Barrido cada {SWEEP_SECONDS/60:.0f} min, hora local {TZ_LABEL}.\n"
            "Publica de a uno y respetando el espaciado mínimo, igual que antes.\n"
            "Mándame foto + descripción cuando quieras publicar algo a mano."
        )

    ciclo = 0
    fallos_seguidos = 0
    ya_avise_del_fallo = False
    reinicio_pedido = False
    limite = time.monotonic() + MAX_RUNTIME_SECONDS if MAX_RUNTIME_SECONDS else None

    proximo_selfcheck = None
    if SELFCHECK_HOURS:
        proximo_selfcheck = leer_reloj_selfcheck()
        if not proximo_selfcheck:
            proximo_selfcheck = time.time() + SELFCHECK_HOURS * 3600
            guardar_reloj_selfcheck(proximo_selfcheck)

    while not _parar:
        if limite and time.monotonic() >= limite:
            log("Se cumplió el tiempo del turno; cierro para que entre el relevo.")
            break
        ciclo += 1
        # Telegram primero: lo que mandas a mano tiene prioridad sobre el barrido.
        ok_tg = correr("telegram_listener.py")

        # Botón 🔄 Reiniciar del chat: el listener deja la orden escrita y acá
        # se recoge. Cerrar el turno ES el reinicio: el relevo ya encolado
        # arranca solo en cuanto se libera el puesto. Se limpia la orden ANTES
        # de guardar, para que el turno nuevo no la vuelva a encontrar y se
        # reinicie en bucle.
        if cola.hay_reinicio():
            log("Reinicio pedido desde el chat; cierro el turno para que entre uno nuevo.")
            cola.limpiar_reinicio()
            guardar_estado()
            reinicio_pedido = True
            break

        if _parar:
            break
        ok_poll = correr("poll_and_publish.py")

        # Se sube el registro ahora mismo, no al final del turno.
        guardar_estado()

        if ok_tg and ok_poll:
            if fallos_seguidos and ya_avise_del_fallo:
                avisar_telegram("🟢 Ya se recuperó: el bot volvió a correr sin errores.")
            fallos_seguidos = 0
            ya_avise_del_fallo = False
        else:
            fallos_seguidos += 1
            log(f"Ciclo {ciclo} con fallos ({fallos_seguidos} seguidos).")
            if fallos_seguidos >= FALLOS_PARA_AVISAR and not ya_avise_del_fallo:
                avisar_telegram(
                    f"⚠️ El bot lleva {fallos_seguidos} ciclos seguidos fallando.\n\n"
                    "Suele ser un token de Facebook vencido o el servidor sin internet.\n"
                    "Para ver el detalle, entra al servidor y corre:\n"
                    "revisa los logs del job"
                )
                ya_avise_del_fallo = True

        if proximo_selfcheck and time.time() >= proximo_selfcheck and not _parar:
            log("Toca el autochequeo periódico.")
            correr("selfcheck.py")
            proximo_selfcheck = time.time() + SELFCHECK_HOURS * 3600
            guardar_reloj_selfcheck(proximo_selfcheck)

        if _parar:
            break

        # Jitter: nunca exactamente el mismo intervalo. Un patrón perfectamente
        # regular es justo lo que delata a un bot.
        espera = SWEEP_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
        espera = max(30.0, espera)
        if limite:
            queda = limite - time.monotonic()
            if queda <= 20:
                log("Casi se acaba el turno; cierro para que entre el relevo.")
                break
            espera = min(espera, queda)
        log(f"Ciclo {ciclo} terminado; siguiente en {espera:.0f}s.")
        # Durante la espera se sigue atendiendo el chat. Si mientras tanto
        # pediste reiniciar, se corta acá mismo en vez de esperar el barrido.
        if esperar_atendiendo(espera):
            if cola.hay_reinicio():
                log("Reinicio pedido desde el chat; cierro el turno para que entre uno nuevo.")
                cola.limpiar_reinicio()
                guardar_estado()
                reinicio_pedido = True
            break

    log("Cerrando limpiamente.")
    if not QUIET_LIFECYCLE:
        avisar_telegram(
            "🔄 Turno cerrado porque pediste reiniciar. El nuevo entra en seguida; "
            "te aviso con el 🟢 cuando esté arriba."
            if reinicio_pedido else
            "🔴 El bot se detuvo. Si no fuiste tú, revísalo."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
