#!/usr/bin/env python3
"""Diagnóstico rápido de las credenciales de las páginas.

Corre como paso propio (corto) para que su log se vea al instante en Actions,
en vez de quedar enterrado dentro del proceso largo. No imprime ningún token:
solo el id, el nombre y, si algo falla, el mensaje exacto que devuelve Graph.
"""
import os
import sys

import requests

VERSION = os.environ.get("GRAPH_VERSION") or "v25.0"


def revisar(etiqueta, id_var, token_var):
    page_id = (os.environ.get(id_var) or "").strip()
    token = (os.environ.get(token_var) or "").strip()
    if not page_id or not token:
        print(f"{etiqueta}: falta {id_var} o {token_var}.")
        return
    print(f"{etiqueta}: id termina en …{page_id[-4:]} (largo {len(page_id)}), "
          f"token de largo {len(token)}.")

    # 1) ¿El token sirve, sea de quien sea?
    r = requests.get(
        f"https://graph.facebook.com/{VERSION}/me",
        params={"fields": "id,name", "access_token": token},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"  /me -> {r.status_code}: {r.text[:500]}")
    else:
        d = r.json()
        print(f"  /me -> OK: el token pertenece a «{d.get('name')}» (id …{str(d.get('id'))[-4:]}).")
        if str(d.get("id")) != page_id:
            print("  OJO: el id del token NO coincide con el id configurado.")

    # 2) ¿Se puede leer la página con ese id?
    r = requests.get(
        f"https://graph.facebook.com/{VERSION}/{page_id}",
        params={"fields": "id,name,followers_count", "access_token": token},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"  /{'{id}'} -> {r.status_code}: {r.text[:500]}")
    else:
        d = r.json()
        print(f"  /id -> OK: «{d.get('name')}», {d.get('followers_count')} seguidores.")

    # 3) La llamada que está fallando de verdad.
    r = requests.get(
        f"https://graph.facebook.com/{VERSION}/{page_id}/posts",
        params={"fields": "id,created_time", "limit": 1, "access_token": token},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"  /posts -> {r.status_code}: {r.text[:500]}")
    else:
        print(f"  /posts -> OK: {len(r.json().get('data', []))} post(s) leído(s).")


def main():
    revisar("Página 1 (origen)", "PAGE_ID_MAIN", "PAGE_TOKEN_MAIN")
    print()
    revisar("Página 2 (destino)", "PAGE_ID_BACKUP", "PAGE_TOKEN_BACKUP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
