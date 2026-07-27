#!/usr/bin/env python3
"""Ejecuta comandos administrativos disparados desde el MENÚ DE CONTROL.

Se invoca vía GitHub Actions (.github/workflows/command.yml) con estas variables
de entorno (que el menú manda como inputs del workflow):

  ACTION           refresh_status | delete_backup | edit_caption | regen_image | schedule
  POST_ID          id del post de CAM1 (delete_backup / edit_caption / regen por CAM1)
  SOURCE_POST_ID   id del post original de TV Info (regen_image / schedule directos)
  CAPTION          texto nuevo (edit_caption) o descripción para regen/schedule
  SCHEDULE_TIME    epoch unix para publicación programada (schedule)

Resultados que deja para que el menú los lea:
  status/panel_status.json   -> lista de posts recientes de CAM1 + mapeo publicado
  corrections/<id>.jpg/.txt  -> imagen corregida lista para descargar (regen_image)
"""
import os
import sys
import json
import tempfile
from pathlib import Path

import requests
import poll_and_publish as bot

BASE_DIR = Path(__file__).resolve().parent
STATUS_DIR = BASE_DIR / "status"
CORR_DIR = BASE_DIR / "corrections"


def log(msg):
    print(f"[cmd] {msg}", flush=True)


def refresh_status():
    STATUS_DIR.mkdir(exist_ok=True)
    bfields = "id,message,created_time,permalink_url,full_picture,is_published"
    backup = bot.graph_get(
        f"{bot.PAGE_ID_BACKUP}/posts", bot.PAGE_TOKEN_BACKUP, fields=bfields, limit=15
    ).get("data", [])
    mfields = "id,message,created_time,full_picture,attachments{media_type}"
    main = bot.graph_get(
        f"{bot.PAGE_ID_MAIN}/posts", bot.PAGE_TOKEN_MAIN, fields=mfields, limit=10
    ).get("data", [])
    # posts programados (aún sin publicar) en TV Info -> para previsualizar lo que viene
    scheduled = []
    try:
        scheduled = bot.graph_get(
            f"{bot.PAGE_ID_MAIN}/scheduled_posts",
            bot.PAGE_TOKEN_MAIN,
            fields="id,message,scheduled_publish_time,created_time",
            limit=25,
        ).get("data", [])
    except Exception as e:
        log(f"No se pudieron leer los programados de TV Info: {e}")
    pubmap = {}
    if bot.PUBLISHED_MAP_PATH.exists():
        try:
            pubmap = json.loads(bot.PUBLISHED_MAP_PATH.read_text())
        except Exception:
            pubmap = {}
    status = {
        "backup_posts": backup,
        "main_posts": main,
        "scheduled_main": scheduled,
        "published_map": pubmap,
    }
    (STATUS_DIR / "panel_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2)
    )
    log(f"Status: {len(backup)} en CAM1, {len(main)} publicados y {len(scheduled)} programados en TV Info.")


def delete_backup(post_id):
    if not post_id:
        log("delete_backup: falta POST_ID."); return
    url = f"https://graph.facebook.com/{bot.GRAPH_VERSION}/{post_id}"
    r = requests.delete(url, params={"access_token": bot.PAGE_TOKEN_BACKUP}, timeout=30)
    r.raise_for_status()
    log(f"Post {post_id} borrado: {r.json()}")


def edit_caption(post_id, caption):
    if not post_id:
        log("edit_caption: falta POST_ID."); return
    url = f"https://graph.facebook.com/{bot.GRAPH_VERSION}/{post_id}"
    r = requests.post(
        url, data={"message": caption, "access_token": bot.PAGE_TOKEN_BACKUP}, timeout=30
    )
    r.raise_for_status()
    log(f"Descripción de {post_id} actualizada.")


def resolve_source(source_post_id, backup_post_id):
    if source_post_id:
        return source_post_id, None
    if backup_post_id and bot.PUBLISHED_MAP_PATH.exists():
        try:
            m = json.loads(bot.PUBLISHED_MAP_PATH.read_text())
            entry = m.get(str(backup_post_id))
            if entry:
                return entry.get("source_post_id"), entry.get("source_text")
        except Exception:
            pass
    return None, None


def _compose_from_source(source_post_id, source_text, caption_override, tmpdir):
    fields = "id,message,attachments{media_type,type,media,subattachments{media,type}}"
    post = bot.graph_get(source_post_id, bot.PAGE_TOKEN_MAIN, fields=fields)
    kind, images = bot.classify_attachment(post)
    if kind != "photo" or not images:
        log("El post fuente no tiene fotos aprovechables.")
        return None, None
    text = (post.get("message") or source_text or "").strip()
    local = []
    for i, u in enumerate(images):
        d = tmpdir / f"src_{i}.jpg"
        bot.download_image(u, d)
        local.append(d)
    edit = bot.ask_claude(text, len(local))
    if edit.get("skip"):
        log(f"Claude omitió el post ({edit.get('skip_reason')}).")
        return None, None
    spec = bot.build_compose_spec(local, edit, tmpdir)
    out = tmpdir / "out.jpg"
    bot.compose_image(spec, out)
    caption = caption_override or edit.get("caption", "")
    return out, caption


def regen_image(source_post_id, source_text, caption_override):
    if not source_post_id:
        log("regen_image: no se pudo resolver el post original."); return
    CORR_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        out, caption = _compose_from_source(source_post_id, source_text, caption_override, tmpdir)
        if not out:
            return
        stub = str(source_post_id).split("_")[-1]
        dest = CORR_DIR / f"{stub}.jpg"
        import shutil
        shutil.copy(out, dest)
        (CORR_DIR / f"{stub}.txt").write_text(caption or "", encoding="utf-8")
    log(f"regen_image: imagen corregida lista para descargar: corrections/{stub}.jpg")


def schedule_post(source_post_id, source_text, schedule_time, caption_override):
    if not source_post_id or not schedule_time:
        log("schedule: falta SOURCE_POST_ID o SCHEDULE_TIME."); return
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        out, caption = _compose_from_source(source_post_id, source_text, caption_override, tmpdir)
        if not out:
            return
        url = f"https://graph.facebook.com/{bot.GRAPH_VERSION}/{bot.PAGE_ID_BACKUP}/photos"
        with open(out, "rb") as f:
            r = requests.post(
                url,
                files={"source": f},
                data={
                    "caption": caption,
                    "published": "false",
                    "scheduled_publish_time": str(int(float(schedule_time))),
                    "access_token": bot.PAGE_TOKEN_BACKUP,
                },
                timeout=60,
            )
        r.raise_for_status()
        log(f"schedule: post programado en CAM1: {r.json()}")


def main():
    action = os.environ.get("ACTION", "refresh_status").strip()
    post_id = os.environ.get("POST_ID", "").strip()
    source_post_id = os.environ.get("SOURCE_POST_ID", "").strip()
    caption = os.environ.get("CAPTION", "")
    sched = os.environ.get("SCHEDULE_TIME", "").strip()
    log(f"ACTION={action} POST_ID={post_id} SOURCE={source_post_id}")

    if action == "refresh_status":
        refresh_status()
    elif action == "delete_backup":
        delete_backup(post_id)
        refresh_status()
    elif action == "edit_caption":
        edit_caption(post_id, caption)
        refresh_status()
    elif action == "regen_image":
        src, txt = resolve_source(source_post_id, post_id)
        regen_image(src, txt, caption)
    elif action == "schedule":
        src, txt = resolve_source(source_post_id, post_id)
        schedule_post(src, txt, sched, caption)
    else:
        log(f"Acción desconocida: {action}")


if __name__ == "__main__":
    main()
