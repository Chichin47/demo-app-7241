"""Publicar en Instagram lo mismo que acaba de salir en la página 2.

La idea es simple y conviene tenerla clara antes de leer el código: a Claude NO
se le pide nada nuevo. La descripción ya está escrita y la foto ya está armada
y publicada en Facebook. Acá solo se agarra eso mismo, ya hecho y ya pagado, y
se manda a una segunda dirección. Publicar en Meta no gasta tokens: son
llamadas gratuitas a su API. Una salida, una escritura, dos destinos.

Reglas de la casa, en orden de importancia:

1. Instagram NUNCA puede romper Facebook. Todo lo de acá va envuelto: si algo
   falla, se anota en el registro y se sigue. El post de la página 2 ya salió
   antes de que esta parte empiece, así que no hay nada que perder.
2. Nada de configuración nueva. La cuenta de Instagram no se pone a mano en
   ningún lado: se le pregunta a Facebook cuál está vinculada a la página.
3. Se puede apagar de un saque con INSTAGRAM=0 en el entorno, sin tocar código.

Por qué hace falta esto y no alcanza con el cruce de Meta: la opción "publicar
en Facebook y también en Instagram" vive DENTRO del creador de publicaciones de
Business Suite, y cruza porque manda a los dos destinos en el momento en que
apretás Publicar. No es una regla de la página que reenvíe lo que sea que se
publique. El bot no pasa por ese creador —le habla directo a la API—, así que
lo que él publica se queda solo en Facebook salvo que se lo mande a mano.
"""

import os

import requests

GRAPH = "https://graph.facebook.com/v25.0"

# Cuánto se espera a cada llamada. Instagram tiene que ir a buscar la imagen a
# la dirección que le pasamos, así que la primera es la más lenta de las dos.
ESPERA = 60

# La cuenta vinculada se pregunta una vez por corrida y queda acá. No cambia de
# un post al otro, y preguntarla en cada publicación sería una llamada al pedo.
_CUENTA = {}


def activo():
    """¿Está encendido? Con INSTAGRAM=0 (o no, off, false) se apaga entero."""
    valor = (os.environ.get("INSTAGRAM") or "1").strip().lower()
    return valor not in ("0", "no", "off", "false", "")


def cuenta(page_id, token, log=print):
    """El identificador de la cuenta de Instagram vinculada a esta página.

    Devuelve None si no hay ninguna vinculada, o si el token no alcanza para
    verla (que es el caso más probable la primera vez: hay que regenerarlo con
    los permisos instagram_basic e instagram_content_publish).
    """
    if page_id in _CUENTA:
        return _CUENTA[page_id]
    ficha = None
    try:
        r = requests.get(
            f"{GRAPH}/{page_id}",
            params={"fields": "instagram_business_account{id,username}",
                    "access_token": token},
            timeout=30,
        )
        if r.status_code >= 400:
            log(f"Instagram: no pude preguntar por la cuenta vinculada "
                f"({r.status_code}): {(r.text or '')[:400]}")
        else:
            ficha = (r.json() or {}).get("instagram_business_account")
    except Exception as e:
        log(f"Instagram: no pude preguntar por la cuenta vinculada ({e}).")
    _CUENTA[page_id] = ficha
    if ficha:
        log(f"Instagram: publico en @{ficha.get('username')} ({ficha.get('id')}).")
    return ficha


def _direccion_de_la_foto(resultado, token, log=print):
    """La dirección pública de la foto que acabamos de publicar en Facebook.

    Instagram no acepta que le subamos el archivo: exige una dirección de
    internet de donde ir a buscarlo. En vez de alquilar un hosting, se usa la
    que Facebook le acaba de dar a esa misma foto al publicarla.
    """
    foto_id = resultado.get("id")
    if not foto_id:
        return None
    try:
        r = requests.get(
            f"{GRAPH}/{foto_id}",
            params={"fields": "images", "access_token": token},
            timeout=30,
        )
        r.raise_for_status()
        imagenes = (r.json() or {}).get("images") or []
        if not imagenes:
            log("Instagram: Facebook no devolvió ninguna dirección para la foto.")
            return None
        # La primera es la más grande; Instagram prefiere la de mayor tamaño.
        return imagenes[0].get("source")
    except Exception as e:
        log(f"Instagram: no pude sacar la dirección de la foto ({e}).")
        return None


# Lo que Instagram acepta: de 4:5 (vertical, 0,80) a 1,91:1 (apaisada).
FORMA_MINIMA = 0.80
FORMA_MAXIMA = 1.91


def forma(ruta, log=print):
    """Mide la foto y dice si Instagram la va a aceptar.

    Esto importa más de lo que parece: la foto compuesta apila las fotos del
    post una debajo de la otra, así que un post de una sola foto apaisada entra
    sin problema, pero uno de tres queda tan alto que Instagram lo rechaza.
    Medirlo acá evita una llamada al pedo y, sobre todo, deja escrito en el
    registro cuántos entran y cuántos no, que es lo que hace falta saber para
    decidir si vale la pena rearmar la imagen para Instagram más adelante.

    Devuelve (entra, proporcion). Si no se puede medir, se deja pasar: que
    conteste Instagram.
    """
    try:
        from PIL import Image
        with Image.open(ruta) as im:
            ancho, alto = im.size
    except Exception as e:
        log(f"Instagram: no pude medir la foto ({e}); la mando igual.")
        return True, None
    proporcion = (ancho / alto) if alto else 0
    entra = FORMA_MINIMA <= proporcion <= FORMA_MAXIMA
    estado = "entra" if entra else "NO entra"
    log(f"Instagram: la foto es {ancho}x{alto}, proporción {proporcion:.2f} "
        f"({estado}; se acepta de {FORMA_MINIMA:.2f} a {FORMA_MAXIMA:.2f}).")
    return entra, proporcion


def publicar_foto(page_id, token, resultado, caption, ruta=None, log=print):
    """Manda a Instagram la foto que ya se publicó en la página 2.

    `resultado` es lo que devolvió publish_photo, tal cual. Devuelve el
    identificador del post de Instagram, o None si no salió; nunca revienta.
    """
    if not activo():
        return None
    ficha = cuenta(page_id, token, log=log)
    if not ficha:
        return None
    ig = ficha.get("id")

    # Se mide antes de molestar a nadie: si la forma no entra, Instagram lo iba
    # a rechazar igual, y así queda claro en el registro por qué no salió.
    if ruta:
        entra, proporcion = forma(ruta, log=log)
        if not entra:
            log("Instagram: no la mando, la rechazaría por la forma. "
                "El post de Facebook ya salió, no se pierde nada.")
            return None

    direccion = _direccion_de_la_foto(resultado, token, log=log)
    if not direccion:
        return None

    try:
        # Paso 1: se arma el envase, que es Instagram yendo a buscar la imagen.
        r = requests.post(
            f"{GRAPH}/{ig}/media",
            data={"image_url": direccion, "caption": caption or "",
                  "access_token": token},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no aceptó la foto ({r.status_code}): "
                f"{(r.text or '')[:500]}")
            return None
        envase = (r.json() or {}).get("id")
        if not envase:
            log("Instagram: no devolvió envase para publicar.")
            return None

        # Paso 2: se publica ese envase. Recién acá aparece en el perfil.
        r = requests.post(
            f"{GRAPH}/{ig}/media_publish",
            data={"creation_id": envase, "access_token": token},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no pudo publicar el envase ({r.status_code}): "
                f"{(r.text or '')[:500]}")
            return None
        ig_post = (r.json() or {}).get("id")
        log(f"Instagram: publicado {ig_post} en @{ficha.get('username')}.")
        return ig_post
    except Exception as e:
        log(f"Instagram: falló la publicación ({e}).")
        return None


def diagnostico(page_id, token):
    """Un informe corto y en castellano de si esto puede funcionar o no.

    Sirve para no andar adivinando si al token le faltan permisos: se pregunta
    y Facebook contesta. Devuelve texto listo para mandar por Telegram.
    """
    if not activo():
        return "🚫 Instagram apagado a propósito (INSTAGRAM=0 en el entorno)."

    lineas = []
    try:
        r = requests.get(
            f"{GRAPH}/{page_id}",
            params={"fields": "name,instagram_business_account{id,username,followers_count}",
                    "access_token": token},
            timeout=30,
        )
        datos = r.json() if r.content else {}
    except Exception as e:
        return f"❌ No pude preguntarle nada a Facebook: {e}"

    if r.status_code >= 400:
        error = ((datos.get("error") or {}).get("message") or "")[:300]
        lineas.append(f"❌ Facebook rechazó la consulta ({r.status_code}).")
        if error:
            lineas.append(f"Dice: {error}")
        lineas.append("")
        lineas.append("Casi seguro le faltan permisos al token de la página 2. "
                      "Hay que regenerarlo agregando instagram_basic e "
                      "instagram_content_publish.")
        return "\n".join(lineas)

    ficha = datos.get("instagram_business_account")
    lineas.append(f"📘 Página: {datos.get('name') or page_id}")
    if not ficha:
        lineas.append("")
        lineas.append("❌ No veo ninguna cuenta de Instagram vinculada.")
        lineas.append("")
        lineas.append("Puede ser una de dos: o la vinculación no llegó a "
                      "guardarse, o el token no tiene permiso para verla. Si en "
                      "Business Suite la ves conectada, entonces es el token: "
                      "hay que regenerarlo con instagram_basic e "
                      "instagram_content_publish.")
        return "\n".join(lineas)

    lineas.append(f"📸 Instagram: @{ficha.get('username')} ({ficha.get('id')})")
    seguidores = ficha.get("followers_count")
    if seguidores:
        lineas.append(f"👥 {seguidores:,} seguidores".replace(",", "."))
    lineas.append("")

    # Ver la cuenta no alcanza: publicar pide otro permiso. Se comprueba
    # pidiéndole el listado de publicaciones, que exige lo mismo que publicar.
    try:
        r2 = requests.get(
            f"{GRAPH}/{ficha['id']}/media",
            params={"limit": 1, "access_token": token},
            timeout=30,
        )
        if r2.status_code >= 400:
            detalle = ((r2.json().get("error") or {}).get("message") or "")[:300]
            lineas.append("⚠️ La veo, pero el token no llega a su contenido.")
            if detalle:
                lineas.append(f"Dice: {detalle}")
            lineas.append("")
            lineas.append("Falta el permiso instagram_content_publish.")
        else:
            lineas.append("✅ Todo en orden: puedo publicar en esa cuenta.")
    except Exception as e:
        lineas.append(f"⚠️ No pude comprobar el permiso de publicación: {e}")

    return "\n".join(lineas)
