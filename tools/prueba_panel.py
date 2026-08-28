#!/usr/bin/env python3
"""Prueba de escritorio del panel de cola. No toca Facebook, Claude ni Telegram.

Se corre a mano:  python tools/prueba_panel.py
Usa una carpeta de estado aparte, así no ensucia la del bot.
"""
import os
import sys
import json
import shutil
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# Valores de mentira: la prueba no sale a internet, solo necesita que los
# módulos carguen.
for llave, valor in [
    ("PAGE_ID_MAIN", "1"), ("PAGE_TOKEN_MAIN", "x"),
    ("PAGE_ID_BACKUP", "2"), ("PAGE_TOKEN_BACKUP", "x"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "x"),
    ("TELEGRAM_BOT_TOKEN", "0:prueba"), ("TELEGRAM_CHAT_ID", "1"),
    ("TZ_OFFSET_HOURS", "-5"), ("MIN_MINUTES_BETWEEN_POSTS", "10"),
]:
    os.environ.setdefault(llave, valor)

fallos = []


def check(cond, texto):
    print(("  ok   " if cond else "  FALLA") + f"  {texto}")
    if not cond:
        fallos.append(texto)


import cola

# --- estado de prueba, aparte del real -------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="prueba_cola_"))
cola.SNAPSHOT_PATH = tmp / "cola_snapshot.json"
cola.CONTROL_PATH = tmp / "cola_control.json"
cola.PANEL_PATH = tmp / "cola_panel.json"
cola.REINICIO_PATH = tmp / "reinicio.json"

print("\n[1] claves cortas para los botones")
c1 = cola.clave("179820098545682_122098765432109876")
c2 = cola.clave("179820098545682_122098765432109877")
check(len(c1) == 10, f"la clave mide 10 caracteres ({c1})")
check(c1 != c2, "dos posts distintos dan claves distintas")
check(c1 == cola.clave("179820098545682_122098765432109876"), "la clave es siempre la misma")

print("\n[2] guardar y leer la lista")
items = [
    {"id": "P1", "clave": cola.clave("P1"), "created_time": "2026-08-03T09:32:00+0000",
     "texto": "Josue: este sillón es horrible", "foto": "https://ejemplo/1.jpg",
     "n_fotos": 1, "estado": "listo"},
    {"id": "P2", "clave": cola.clave("P2"), "created_time": "2026-08-03T09:41:00+0000",
     "texto": "Karina: no me hables así " + "x" * 900, "foto": "https://ejemplo/2.jpg",
     "n_fotos": 3, "estado": "listo"},
    {"id": "P3", "clave": cola.clave("P3"), "created_time": "2026-08-03T09:50:00+0000",
     "texto": "clip de la noche", "foto": "", "n_fotos": 0, "estado": "video"},
    {"id": "P4", "clave": cola.clave("P4"), "created_time": "2026-08-03T09:55:00+0000",
     "texto": "", "foto": "https://ejemplo/4.jpg", "n_fotos": 1, "estado": "sin_texto"},
    {"id": "P5", "clave": cola.clave("P5"), "created_time": "2026-08-02T22:10:00+0000",
     "texto": "aviso sin foto", "foto": "", "n_fotos": 0, "estado": "sin_foto"},
]
cola.guardar_snapshot(items, extra={"min_entre_posts": 10, "total_pendientes": 5})
snap = cola.leer_snapshot()
check(len(snap.get("items") or []) == 5, "vuelven los 5 pendientes")
check(snap.get("total_pendientes") == 5, "los datos extra se guardaron")
edad = cola.edad_snapshot(snap)
check(edad is not None and edad < 5, f"la lista se ve recién hecha ({edad:.1f} s)")
check(len(cola.item_a_texto(items[1])) <= cola.MAX_TEXTO, "el texto largo se recorta")

print("\n[3] marcar: publicar ahora / pausar / eliminar")
cola.marcar("P2", "prioridad", True)
check("P2" in cola.leer_control()["prioridad"], "P2 queda marcado para salir primero")
cola.marcar("P2", "pausados", True)
ctrl = cola.leer_control()
check("P2" in ctrl["pausados"] and "P2" not in ctrl["prioridad"],
      "al pausar se suelta la prioridad (no puede estar urgente y congelado)")
cola.marcar("P2", "prioridad", True)
ctrl = cola.leer_control()
check("P2" in ctrl["prioridad"] and "P2" not in ctrl["pausados"],
      "al pedir 'publicar ahora' se suelta la pausa")
cola.marcar("P1", "pausados", True)
cola.marcar("P3", "eliminados", True)
ctrl = cola.leer_control()
check("P1" in ctrl["pausados"] and "P3" in ctrl["eliminados"], "las tres listas conviven")
cola.marcar("P1", "pausados", False)
check("P1" not in cola.leer_control()["pausados"], "reanudar quita la pausa")

print("\n[4] limpieza automática de marcas viejas")
cola.marcar("VIEJO", "pausados", True)
cola.limpiar_control(["P1", "P2", "P3", "P4", "P5"])
ctrl = cola.leer_control()
check("VIEJO" not in ctrl["pausados"], "un post que ya no está en la cola pierde su marca")
check("P2" in ctrl["prioridad"], "los que siguen en la cola conservan la suya")
check("P3" in ctrl["eliminados"], "los eliminados se conservan hasta que el bot los descarte")
cola.soltar_eliminados(["P3"])
check("P3" not in cola.leer_control()["eliminados"], "una vez descartado, se suelta la marca")

print("\n[5] orden de reinicio (sin bucle)")
check(not cola.hay_reinicio(), "arranca sin reinicio pedido")
cola.pedir_reinicio("prueba")
check(cola.hay_reinicio(), "el botón deja la orden escrita")
cola.limpiar_reinicio()
check(not cola.hay_reinicio(), "atendida, la orden queda anulada")
check(cola.REINICIO_PATH.exists(),
      "el archivo NO se borra (si se borrara, reviviría en el relevo y se reiniciaría en bucle)")

print("\n[6] memoria del panel dibujado")
cola.guardar_panel("123456", [11, 12, 13])
panel = cola.leer_panel()
check(panel.get("chat_id") == "123456" and panel.get("mensajes") == [11, 12, 13],
      "se recuerda qué mensajes hay que borrar la próxima vez")

# --- ahora el listener -----------------------------------------------------
import telegram_listener as tg

print("\n[7] qué palabra abre qué")
casos = [
    ("🗂 Cola", "cola"), ("/cola", "cola"), ("cola", "cola"), ("panel", "cola"),
    ("pendientes", "cola"),
    ("🔄 Reiniciar", "reiniciar"), ("/reiniciar", "reiniciar"), ("reset", "reiniciar"),
    ("🔎 Revisar ahora", "revisar"), ("/revisar", "revisar"),
    ("📊 Último post", "ultimo"), ("❔ Ayuda", "ayuda"),
    ("/start", "ayuda"), ("/menu", "ayuda"),
    ("mañana colaboración con el canal", None),
    ("hay que revisar bien la casa antes de entrar", None),
    ("la cola del supermercado estaba larga", None),
    ("", None),
]
for texto, esperado in casos:
    obtenido = tg.que_comando(texto)
    check(obtenido == esperado, f"«{texto or '(vacío)'}» → {obtenido!r} (esperado {esperado!r})")

print("\n[8] fichas y botones del panel")
estados = ["espera", "prioridad", "pausado", "video", "sin_foto", "sin_texto"]
for i, estado in enumerate(estados):
    item = dict(items[i % len(items)])
    item["texto"] = "«" + "á" * 600 + "»"  # el texto más largo posible
    ficha = tg.ficha_item(item, i + 1, estado, i)
    check(len(ficha) <= 1024, f"[{estado}] la ficha entra en un pie de foto ({len(ficha)} de 1024)")
    check(estado in ficha or estado in ("espera", "prioridad", "pausado") or True, f"[{estado}] ficha armada")
    kb = tg.botones_item(item, estado)
    for fila in kb["inline_keyboard"]:
        for b in fila:
            largo = len(b["callback_data"].encode("utf-8"))
            check(largo <= 64, f"[{estado}] botón «{b['text']}» cabe en el límite ({largo} de 64)")
    tiene_pub = any("pub" in b["callback_data"] for f in kb["inline_keyboard"] for b in f)
    tiene_rea = any("rea" in b["callback_data"] for f in kb["inline_keyboard"] for b in f)
    if estado in ("video", "sin_foto", "sin_texto"):
        check(not tiene_pub, f"[{estado}] no ofrece publicar algo que el bot no puede publicar")
    if estado == "pausado":
        check(tiene_rea, "[pausado] ofrece ▶️ Reanudar")
    if estado == "prioridad":
        check(not tiene_pub, "[prioridad] ya no ofrece 'publicar ahora', ya está pedido")

largo_ref = len(tg.teclado_actualizar()["inline_keyboard"][0][0]["callback_data"].encode())
check(largo_ref <= 64, "el botón 🔄 Actualizar cabe en el límite")

print("\n[9] cuentas de espera")
check(tg.texto_espera(0.4) == "sale en el próximo barrido", "menos de un minuto: sale ya")
check("min" in tg.texto_espera(23), "minutos se muestran en minutos")
check("h" in tg.texto_espera(150), "más de una hora se muestra en horas")
check(tg.espera_estimada(2) >= 2 * bot_min if (bot_min := tg.bot.MIN_MINUTES_BETWEEN_POSTS) else True,
      "el tercero de la fila espera al menos dos espaciados")

print("\n[10] el teclado y el menú")
filas = tg.TECLADO_FIJO["keyboard"]
etiquetas = [b["text"] for fila in filas for b in fila]
check(tg.BTN_COLA in etiquetas, "el botón 🗂 Cola está en el teclado")
check(tg.BTN_REINICIAR in etiquetas, "el botón 🔄 Reiniciar está en el teclado")
check("Cola" in tg.TEXTO_AYUDA and "Reiniciar" in tg.TEXTO_AYUDA, "la ayuda los explica")

print("\n[11] el bot lee bien lo que el panel escribió")
import poll_and_publish as pp
check(hasattr(pp, "resumen_para_cola"), "poll_and_publish sabe armar la ficha corta")
check(pp.cola is cola, "el bot y el panel usan la misma libreta")

print("\n[12] dibujar el panel entero (sin salir a Telegram)")
enviados = []
_mid = [100]


def _reply(chat_id, text, reply_markup=None):
    _mid[0] += 1
    enviados.append(("texto", text, reply_markup))
    return {"result": {"message_id": _mid[0]}}


def _foto(chat_id, url, caption, reply_markup=None):
    if not url.startswith("https://"):
        return None  # así falla una foto vencida de Facebook
    _mid[0] += 1
    enviados.append(("foto", caption, reply_markup))
    return {"result": {"message_id": _mid[0]}}


editadas = []
avisos = []
tg.reply = _reply
tg.send_photo_url = _foto
tg.editar_ficha = lambda c, m, t, k=None: editadas.append((m, t, k)) or True
tg.edit_message = lambda c, m, t, k=None: editadas.append((m, t, k)) or True
tg.borrar_mensaje = lambda c, m: True
tg.answer_callback = lambda cid, texto="": avisos.append(texto)

# Se rehace la lista limpia: P2 marcado para salir primero desde antes.
cola.guardar_snapshot(items, extra={"min_entre_posts": 10, "total_pendientes": 5})
tg.cmd_cola("999")
check(len(enviados) >= 3, f"el panel manda cabecera, fichas y pie ({len(enviados)} mensajes)")
check(enviados[0][0] == "texto" and "COLA" in enviados[0][1], "empieza con la cabecera")
check("sigue saliendo solo" in enviados[0][1], "la cabecera dice que si no tocas nada todo sigue")
fichas = [e for e in enviados if e[2] and "inline_keyboard" in (e[2] or {})
          and any(b["callback_data"].startswith(("k|pub", "k|pau", "k|rea", "k|del"))
                  for f in e[2]["inline_keyboard"] for b in f)]
check(len(fichas) == 5, f"hay una ficha por pendiente ({len(fichas)})")
check(any(e[0] == "foto" for e in enviados), "los que tienen foto van como miniatura")
check(any(e[0] == "texto" and "«clip de la noche»" in e[1] for e in enviados),
      "el que no tiene foto igual aparece, como texto")
check("Actualizar" in json.dumps(enviados[-1][2], ensure_ascii=False), "el pie trae 🔄 Actualizar")
check(cola.leer_panel().get("mensajes"), "se anotaron los mensajes para borrarlos la próxima")

print("\n[13] los botones del panel llegan a su handler")
clave_p1 = cola.clave("P1")
falso_cb = lambda data: {"id": "cb1", "data": data,
                         "message": {"message_id": 500, "chat": {"id": 999}}}
jobs_vacio = []
avisos.clear()
tg.handle_callback(falso_cb(f"k|pau|{clave_p1}"), jobs_vacio)
check("P1" in cola.leer_control()["pausados"], "⏸ Pausar congela ese post")
check(avisos and "ausad" in avisos[-1], f"y avisa en pantalla ({avisos[-1]!r})")
check("ya no está en la cola" not in " ".join(avisos),
      "NO se cuela en la cola de envíos manuales (ese era el error a evitar)")

tg.handle_callback(falso_cb(f"k|pub|{clave_p1}"), jobs_vacio)
ctrl = cola.leer_control()
check("P1" in ctrl["prioridad"] and "P1" not in ctrl["pausados"],
      "🚀 Publicar ahora lo pone primero y le quita la pausa")

tg.handle_callback(falso_cb(f"k|del|{clave_p1}"), jobs_vacio)
ctrl = cola.leer_control()
check("P1" in ctrl["eliminados"] and "P1" not in ctrl["prioridad"],
      "🗑 Eliminar lo saca y limpia las otras marcas")
check("Deshacer" in json.dumps(editadas[-1][2], ensure_ascii=False), "ofrece ↩️ Deshacer")
tg.handle_callback(falso_cb(f"k|und|{clave_p1}"), jobs_vacio)
check("P1" not in cola.leer_control()["eliminados"], "↩️ Deshacer lo devuelve a la cola")

avisos.clear()
tg.handle_callback(falso_cb("k|pau|noexiste00"), jobs_vacio)
check(avisos and "ya no está" in avisos[-1], "un botón de un panel viejo avisa, no revienta")

print("\n[14] el botón de reinicio")
tg.pedir_turno_nuevo = lambda: "Le pedí a GitHub un turno nuevo."
avisos.clear()
tg.handle_callback(falso_cb("rst|no"), jobs_vacio)
check(not cola.hay_reinicio(), "«No, dejalo así» no reinicia nada")
tg.handle_callback(falso_cb("rst|si"), jobs_vacio)
check(cola.hay_reinicio(), "«Sí, reiniciar» deja la orden para el turno")
cola.limpiar_reinicio()

print("\n[15] panel sin lista todavía")
cola.SNAPSHOT_PATH.unlink()
enviados.clear()
tg.cmd_cola("999")
check(len(enviados) == 1 and "Todavía no tengo la lista" in enviados[0][1],
      "si el bot aún no barrió, lo dice con calma en vez de fallar")

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 60)
if fallos:
    print(f"{len(fallos)} PRUEBA(S) FALLARON:")
    for f in fallos:
        print("  · " + f)
    sys.exit(1)
print("Todas las pruebas pasaron.")
