#!/usr/bin/env python3
"""Sonda de ScrapeCreators: ¿sirve para leer la página 1 sin token de Meta?

Esto NO es parte del bot y no publica nada. Es una sola llamada de prueba, que
gasta UN crédito, para contestar la única pregunta que los documentos de
ScrapeCreators no contestan: cuando el post trae fotos, ¿devuelven las
direcciones de esas fotos, y las devuelven TODAS?

Importa porque los posts de la página 1 casi siempre traen dos fotos apaisadas
y el bot las necesita a las dos para armar la imagen apilada. En todos los
ejemplos publicados el post es un video y el único campo de imagen que se ve es
"image_url", en singular y en null. Con eso no alcanza para decidir, así que se
mira de verdad.

Se corre a mano desde la pestaña Actions. La llave viaja en un encabezado y
NUNCA se escribe en el log: acá no se imprime ninguna variable de entorno, y lo
único que se muestra del pedido es la dirección, que no la lleva.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.scrapecreators.com/v1/facebook/profile/posts"
LIMA = timezone(timedelta(hours=-5))

# Cómo se reconoce una dirección de foto de Facebook. Se busca por todo el
# árbol de la respuesta, sin suponer en qué campo la guardaron.
ES_FOTO = re.compile(r"(scontent|fbcdn)", re.IGNORECASE)
ES_VIDEO = re.compile(r"\.mp4|video", re.IGNORECASE)


def hora(marca):
    """La fecha del post en hora de Lima, venga como número o como texto."""
    try:
        return datetime.fromtimestamp(float(marca), LIMA).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return str(marca)[:19] if marca else "?"


def buscar_direcciones(nodo, camino=""):
    """Recorre toda la respuesta y devuelve (camino, dirección) de cada enlace.

    Se recorre entero a propósito. Si las fotos vinieran dentro de un arreglo
    de adjuntos, o anidadas tres niveles abajo, un vistazo a los campos de
    arriba no las vería y concluiríamos que no están cuando sí están.
    """
    encontrados = []
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            encontrados += buscar_direcciones(valor, f"{camino}.{clave}" if camino else clave)
    elif isinstance(nodo, list):
        for i, valor in enumerate(nodo):
            encontrados += buscar_direcciones(valor, f"{camino}[{i}]")
    elif isinstance(nodo, str) and nodo.startswith("http"):
        encontrados.append((camino, nodo))
    return encontrados


def main():
    llave = (os.environ.get("SCRAPE_API_KEY") or "").strip()
    if not llave:
        print("❌ Falta el secreto SCRAPE_API_KEY. Sin eso no puedo preguntar nada.")
        return 1
    pagina = (os.environ.get("PAGINA") or "").strip()
    if not pagina:
        print("❌ Falta decirme qué página mirar (PAGINA).")
        return 1

    # pageId si son solo números; si no, se manda como dirección.
    parametro = "pageId" if pagina.isdigit() else "url"
    print(f"→ Preguntando por {parametro}={pagina}\n", flush=True)

    try:
        r = requests.get(API, params={parametro: pagina},
                         headers={"x-api-key": llave}, timeout=60)
    except Exception as e:
        print(f"❌ No se pudo llegar al servicio: {e}")
        return 1

    print(f"Respuesta HTTP {r.status_code}")
    if r.status_code >= 400:
        print(f"❌ Contestó con error: {(r.text or '')[:500]}")
        return 1

    try:
        datos = r.json()
    except Exception:
        print(f"❌ No devolvió JSON. Empieza así: {(r.text or '')[:300]}")
        return 1

    print(f"Créditos cobrados por esta llamada: {datos.get('credits_charged')}")
    print(f"Créditos que te quedan: {datos.get('credits_remaining')}")

    posts = datos.get("posts") or datos.get("data") or []
    if not isinstance(posts, list) or not posts:
        print("\n⚠️ No vinieron posts. La respuesta completa, recortada:")
        print(json.dumps(datos, ensure_ascii=False)[:2000])
        return 1

    print(f"Posts que trajo en esta sola llamada: {len(posts)}")
    print(f"¿Hay cursor para pedir más? {'sí' if datos.get('cursor') else 'no'}")

    # ---------------------------------------------------------------- resumen
    print("\n" + "=" * 70)
    print("LO QUE TRAJO, POST POR POST")
    print("=" * 70)
    con_foto = 0
    for i, post in enumerate(posts[:12], start=1):
        enlaces = buscar_direcciones(post)
        fotos = [(c, u) for c, u in enlaces if ES_FOTO.search(u) and not ES_VIDEO.search(u)]
        videos = [(c, u) for c, u in enlaces if ES_VIDEO.search(u)]
        texto = (post.get("text") or post.get("description") or "").strip()
        texto = " ".join(texto.split())
        if fotos:
            con_foto += 1
        print(f"\n{i}. id={post.get('id') or post.get('post_id')}  "
              f"{hora(post.get('publishTime') or post.get('creation_time'))}")
        print(f"   texto: {texto[:90] or '(vacío)'}")
        print(f"   fotos encontradas: {len(fotos)}   videos: {len(videos)}")
        for camino, _ in fotos[:6]:
            print(f"      · foto en el campo: {camino}")

    print("\n" + "=" * 70)
    print("VEREDICTO")
    print("=" * 70)
    varias = [p for p in posts
              if len([1 for c, u in buscar_direcciones(p)
                      if ES_FOTO.search(u) and not ES_VIDEO.search(u)]) >= 2]
    print(f"De {len(posts[:12])} posts mirados, {con_foto} traen al menos una foto.")
    print(f"Posts con DOS o más fotos: {len(varias)}.")
    if not con_foto:
        print("→ NO sirve: sin direcciones de foto el bot no puede armar la imagen.")
    elif not varias:
        print("→ SIRVE A MEDIAS: hay una foto por post, pero no las dos. Los posts "
              "de dos fotos saldrían con una sola.")
    else:
        print("→ SIRVE: vienen varias fotos por post, que es lo que el bot necesita.")

    # ------------------------------------------------ un post entero, para ver
    ejemplo = next((p for p in posts
                    if [1 for c, u in buscar_direcciones(p)
                        if ES_FOTO.search(u) and not ES_VIDEO.search(u)]), posts[0])
    print("\n" + "=" * 70)
    print("UN POST COMPLETO, TAL CUAL LO DEVUELVEN (recortado)")
    print("=" * 70)
    print(json.dumps(ejemplo, ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
