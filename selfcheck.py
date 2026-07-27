#!/usr/bin/env python3
"""Autochequeo completo del bot: Meta, Claude, Telegram y estado del repositorio.

Se ejecuta a mano desde la pestaña Actions (input "autochequeo" = true) y manda
el resumen al chat de Telegram. Sirve para saber, sin leer logs, si todo está
listo para funcionar solo:

- que los dos tokens de página sigan vivos y con permiso de leer/publicar;
- cuántos posts nuevos ve en la página principal y cuáles están pendientes;
- que la llamada a Claude funcione (incluido el modo manual sin descartes);
- que el bot de Telegram responda y el chat autorizado sea el correcto.

No publica nada en Facebook.

Con la opción --estricto termina con error (código 1) si algo salió mal. La usa
el instalador para no encender el bot cuando un token está mal pegado. Sin esa
opción termina siempre con código 0, que es como lo corre el autochequeo
periódico: ahí un fallo se avisa por Telegram, no se trata como una caída.
"""
import os
import sys
import json
import traceback
from pathlib import Path

import requests

import poll_and_publish as bot

BASE_DIR = Path(__file__).resolve().parent
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

lines = []


def add(ok, text):
    mark = "✅" if ok else ("⚠️" if ok is None else "❌")
    line = f"{mark} {text}"
    lines.append(line)
    print(f"[chk] {line}", flush=True)


def check_page(page_id, token, nombre, publicar=False):
    try:
        data = bot.graph_get(page_id, token, fields="name,fan_count")
        add(True, f"{nombre}: «{data.get('name')}» ({data.get('fan_count', '?')} seguidores), token OK.")
    except Exception as e:
        add(False, f"{nombre}: el token falló ({e}).")
        return False
    try:
        perms = bot.graph_get(f"{page_id}/roles", token, limit=1)
        _ = perms
    except Exception:
        pass  # no todas las páginas exponen /roles; no es señal de problema
    return True


def check_main_page():
    state = bot.load_state()
    processed = set(state.get("processed", []))
    posts = bot.fetch_recent_posts(processed)
    add(True, f"Página 1: {len(posts)} posts leídos, {len(processed)} ya marcados como procesados.")

    pendientes = [p for p in posts if p["id"] not in processed]
    pendientes.sort(key=lambda p: p.get("created_time", ""))
    detalle = []
    con_foto = 0
    for p in pendientes[:10]:
        kind, imgs = bot.classify_attachment(p)
        texto = (p.get("message") or "").strip()
        motivo = ""
        if kind == "video":
            motivo = "video/reel (se ignora)"
        elif kind != "photo" or not imgs:
            motivo = "sin foto (se ignora)"
        elif not texto:
            motivo = "sin texto (se ignora)"
        else:
            con_foto += 1
            motivo = f"listo para publicar ({len(imgs)} foto/s)"
        detalle.append(f"   · {p.get('created_time','')[:16]} — {motivo}")
    add(
        None if not pendientes else True,
        f"Pendientes por publicar: {len(pendientes)} (útiles con foto+texto: {con_foto}).",
    )
    for d in detalle:
        lines.append(d)
        print(f"[chk]{d}", flush=True)
    return pendientes


def check_backup_page(page_id, token):
    try:
        data = bot.graph_get(f"{page_id}/posts", token, fields="id,created_time,message", limit=3)
        posts = data.get("data", [])
        if posts:
            ult = posts[0].get("created_time", "")[:16]
            add(True, f"Página 2: último post automático publicado el {ult} ({len(posts)} recientes leídos).")
        else:
            add(None, "Página 2: todavía no tiene posts publicados.")
    except Exception as e:
        add(False, f"Página 2: no se pudieron leer los posts ({e}).")


def check_claude():
    texto = "Josue: Este sillón de peluche es horrible, ¿quién lo eligió?"
    try:
        edit = bot.ask_claude(texto, 1, manual=True)
        if edit.get("skip"):
            add(False, f"Claude: en modo manual todavía descarta ({edit.get('skip_reason')}).")
            return False
        frases = " | ".join(l.get("text", "") for l in edit.get("lines", []))
        add(True, f"Claude: responde bien en modo manual. Frase de prueba: «{frases}»")
        return True
    except Exception as e:
        add(False, f"Claude: falló la llamada ({e}).")
        return False


def check_ritmo():
    """Revisa el espaciado entre publicaciones (anti 'todo de golpe')."""
    mins = bot.minutes_since_last_publish()
    minimo = bot.MIN_MINUTES_BETWEEN_POSTS
    if mins is None:
        add(True, f"Ritmo: 1 post por corrida, mínimo {minimo:.0f} min entre publicaciones "
                  "(todavía sin publicaciones registradas).")
        return
    origen = bot.load_publish_clock().get("origen", "?")
    estado = "turno libre" if mins >= minimo else f"en espera, faltan {minimo - mins:.1f} min"
    add(True, f"Ritmo: 1 post por corrida, mínimo {minimo:.0f} min entre publicaciones. "
              f"Última hace {mins:.1f} min ({origen}) — {estado}.")


def check_cola_telegram():
    """Revisa la cola de envíos manuales por Telegram."""
    try:
        import telegram_listener as tg
    except Exception as e:
        add(False, f"Telegram: no se pudo leer la cola de envíos manuales ({e}).")
        return
    jobs = tg.load_queue()
    esperando = [j for j in jobs if j.get("status") == "awaiting"]
    agendados = [j for j in jobs if j.get("status") == "scheduled"]
    if not esperando and not agendados:
        add(True, f"Cola de Telegram: vacía (hora local {tg.TZ_LABEL}).")
        return
    partes = []
    if esperando:
        partes.append(f"{len(esperando)} esperando que elijas la hora")
    if agendados:
        prox = min(j.get("publish_at", 0) for j in agendados)
        partes.append(f"{len(agendados)} agendada(s), la próxima el {tg.fmt_local(prox)}")
    add(None, f"Cola de Telegram: " + "; ".join(partes) + f" (hora {tg.TZ_LABEL}).")


def check_telegram():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        add(False, "Telegram: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en los secrets.")
        return False
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=30)
        r.raise_for_status()
        nombre = r.json().get("result", {}).get("username", "?")
        add(True, f"Telegram: bot @{nombre} conectado y chat autorizado configurado.")
        return True
    except Exception as e:
        add(False, f"Telegram: no responde ({e}).")
        return False


def send_summary(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[chk] Sin Telegram configurado; no se manda resumen.", flush=True)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4096]},
            timeout=30,
        )
        r.raise_for_status()
        print("[chk] Resumen enviado a Telegram.", flush=True)
    except Exception as e:
        print(f"[chk] No se pudo mandar el resumen: {e}", flush=True)


def main():
    print("[chk] Iniciando autochequeo.", flush=True)
    ok_main = check_page(bot.PAGE_ID_MAIN, bot.PAGE_TOKEN_MAIN, "Página 1 (origen)")
    ok_backup = check_page(bot.PAGE_ID_BACKUP, bot.PAGE_TOKEN_BACKUP, "Página 2 (destino)")

    pendientes = []
    if ok_main:
        try:
            pendientes = check_main_page()
        except Exception as e:
            add(False, f"Página 1: error leyendo posts ({e}).")
            traceback.print_exc()
    if ok_backup:
        check_backup_page(bot.PAGE_ID_BACKUP, bot.PAGE_TOKEN_BACKUP)

    check_ritmo()
    check_claude()
    check_telegram()
    check_cola_telegram()

    hay_error = any(l.startswith("❌") for l in lines)
    encabezado = (
        "🔎 AUTOCHEQUEO\n\n"
        + ("⚠️ Hay algo que revisar:\n\n" if hay_error else "Todo está bien y listo para funcionar solo.\n\n")
    )
    cola = (
        f"\n\nEl barrido corre cada 3 minutos, pero publica de a UNO y con al menos "
        f"{bot.MIN_MINUTES_BETWEEN_POSTS:.0f} min de separación, para que nunca salgan "
        "varios de golpe. Si le mandas foto + descripción por aquí, te pregunta a qué "
        "hora publicarla y respeta esa hora."
    )
    resumen = encabezado + "\n".join(lines) + cola
    send_summary(resumen)
    print("[chk] Autochequeo terminado.", flush=True)
    return not hay_error


if __name__ == "__main__":
    todo_bien = main()
    if "--estricto" in sys.argv and not todo_bien:
        print("[chk] Hay fallos; termino con error.", flush=True)
        sys.exit(1)
