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

import json
import os
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com/v25.0"

CUENTA_PATH = Path(__file__).resolve().parent / "state" / "instagram.json"

# Un carrusel de Instagram admite de 2 a 10 diapositivas.
CARRUSEL_MAXIMO = 10

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


def token_ig(token_pagina):
    """La llave que se usa para hablar con Instagram.

    Puede ser distinta de la de Facebook, y conviene que lo sea: publicar en
    Instagram pide dos permisos más (instagram_basic e instagram_content_publish)
    y agregárselos a la llave que hoy publica en la página significaría
    rehacerla, con el riesgo de dejar la página muda si algo sale mal.

    Así que si existe el secreto IG_TOKEN se usa ese para Instagram, y la
    llave de la página se queda como está, intacta. Si no existe, se prueba
    con la de la página por si ya tuviera los permisos.
    """
    return (os.environ.get("IG_TOKEN") or "").strip() or token_pagina


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
                    "access_token": token_ig(token)},
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


def anotar(clave):
    """Lleva la cuenta de cómo salió cada post en Instagram.

    Sirve para contestar con datos, y no a ojo, la pregunta de cuántos entran
    apilados y cuántos necesitan carrusel.
    """
    try:
        estado = json.loads(CUENTA_PATH.read_text())
    except Exception:
        estado = {}
    estado[clave] = int(estado.get(clave, 0)) + 1
    estado["total"] = int(estado.get("total", 0)) + 1
    try:
        CUENTA_PATH.parent.mkdir(parents=True, exist_ok=True)
        CUENTA_PATH.write_text(json.dumps(estado, indent=2), encoding="utf-8")
    except Exception:
        pass


def _subir_oculta(page_id, token, ruta, log=print):
    """Sube una foto a Facebook SIN publicarla, solo para tener su dirección.

    Instagram no acepta que le subamos el archivo: exige una dirección de
    internet. Para la imagen apilada alcanza con la dirección de la foto que ya
    se publicó, pero las diapositivas del carrusel no están publicadas en
    ningún lado, así que hay que darles una.

    Con published=false la foto NO sale en la página ni la ve nadie: queda
    guardada y nada más. Devuelve (direccion, id_para_borrarla_despues).
    """
    try:
        with open(ruta, "rb") as f:
            r = requests.post(
                f"{GRAPH}/{page_id}/photos",
                files={"source": f},
                data={"published": "false", "access_token": token},
                timeout=ESPERA,
            )
        if r.status_code >= 400:
            log(f"Instagram: no pude dejar la diapositiva en Facebook "
                f"({r.status_code}): {(r.text or '')[:300]}")
            return None, None
        foto_id = (r.json() or {}).get("id")
        if not foto_id:
            return None, None
        r2 = requests.get(f"{GRAPH}/{foto_id}",
                          params={"fields": "images", "access_token": token},
                          timeout=30)
        r2.raise_for_status()
        imagenes = (r2.json() or {}).get("images") or []
        return (imagenes[0].get("source") if imagenes else None), foto_id
    except Exception as e:
        log(f"Instagram: falló al preparar una diapositiva ({e}).")
        return None, None


def _borrar_ocultas(ids, token, log=print):
    """Borra las copias temporales que acabamos de subir para el carrusel.

    Ojo con lo que borra y lo que no: SOLO toca los identificadores que esta
    misma función acaba de crear segundos antes, que son copias sin publicar
    que nadie vio nunca. Nunca toca una publicación de verdad. Con
    IG_LIMPIAR=0 se puede dejar sin borrar nada.
    """
    if (os.environ.get("IG_LIMPIAR") or "1").strip().lower() in ("0", "no", "off", "false"):
        return
    for foto_id in ids:
        try:
            requests.delete(f"{GRAPH}/{foto_id}",
                            params={"access_token": token}, timeout=30)
        except Exception as e:
            log(f"Instagram: quedó una copia temporal sin borrar ({e}).")


def _carrusel(page_id, token, ig, diapositivas, caption, log=print):
    """Publica varias fotos como un carrusel que se desliza.

    Es el camino de los posts de tres o cuatro fotos: apiladas quedarían
    demasiado altas para Instagram, pero de a una entran perfecto y encima se
    ven más grandes que en la imagen apilada.
    """
    if len(diapositivas) > CARRUSEL_MAXIMO:
        log(f"Instagram: el post trae {len(diapositivas)} fotos y el carrusel "
            f"admite {CARRUSEL_MAXIMO}; mando las primeras {CARRUSEL_MAXIMO}.")
        diapositivas = diapositivas[:CARRUSEL_MAXIMO]

    temporales, hijos = [], []
    try:
        for n, ruta in enumerate(diapositivas, 1):
            direccion, foto_id = _subir_oculta(page_id, token, ruta, log=log)
            if foto_id:
                temporales.append(foto_id)
            if not direccion:
                log(f"Instagram: me quedé sin la diapositiva {n}; no mando el carrusel.")
                return None
            r = requests.post(
                f"{GRAPH}/{ig}/media",
                data={"image_url": direccion, "is_carousel_item": "true",
                      "access_token": token_ig(token)},
                timeout=ESPERA,
            )
            if r.status_code >= 400:
                log(f"Instagram: rechazó la diapositiva {n} ({r.status_code}): "
                    f"{(r.text or '')[:300]}")
                return None
            hijo = (r.json() or {}).get("id")
            if not hijo:
                return None
            hijos.append(hijo)

        if len(hijos) < 2:
            log("Instagram: un carrusel necesita al menos dos fotos.")
            return None

        r = requests.post(
            f"{GRAPH}/{ig}/media",
            data={"media_type": "CAROUSEL", "children": ",".join(hijos),
                  "caption": caption or "", "access_token": token_ig(token)},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no aceptó el carrusel ({r.status_code}): "
                f"{(r.text or '')[:400]}")
            return None
        envase = (r.json() or {}).get("id")
        if not envase:
            return None

        r = requests.post(
            f"{GRAPH}/{ig}/media_publish",
            data={"creation_id": envase, "access_token": token_ig(token)},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no pudo publicar el carrusel ({r.status_code}): "
                f"{(r.text or '')[:400]}")
            return None
        ig_post = (r.json() or {}).get("id")
        log(f"Instagram: publicado {ig_post} como carrusel de {len(hijos)} fotos.")
        return ig_post
    except Exception as e:
        log(f"Instagram: falló el carrusel ({e}).")
        return None
    finally:
        _borrar_ocultas(temporales, token, log=log)


def publicar_foto(page_id, token, resultado, caption, ruta=None,
                  diapositivas=None, log=print):
    """Manda a Instagram lo mismo que acaba de salir en la página 2.

    Dos caminos, y el que se usa lo decide la forma de la imagen, no el gusto:

    * Si la imagen apilada entra en lo que Instagram acepta (una foto, o dos
      apaisadas), va tal cual, igualita a como se ve en Facebook. No hay que
      subir nada: se usa la dirección que Facebook le acaba de dar.
    * Si no entra (tres o cuatro fotos, que apiladas quedan casi 9:16), va como
      carrusel: cada foto una diapositiva, entera y con su propia frase encima.

    `resultado` es lo que devolvió publish_photo, tal cual. Devuelve el
    identificador del post de Instagram, o None si no salió; nunca revienta.
    """
    if not activo():
        return None
    ficha = cuenta(page_id, token, log=log)
    if not ficha:
        return None
    ig = ficha.get("id")

    # Se mide antes de molestar a nadie: si la forma no entra, Instagram la iba
    # a rechazar igual, y así queda escrito en el registro por qué se fue por
    # el otro camino.
    entra = True
    if ruta:
        entra, _ = forma(ruta, log=log)

    if not entra:
        if diapositivas and len(diapositivas) >= 2:
            ig_post = _carrusel(page_id, token, ig, diapositivas, caption, log=log)
            anotar("carrusel" if ig_post else "carrusel_fallado")
            return ig_post
        log("Instagram: no entra apilada y no hay diapositivas para el carrusel. "
            "El post de Facebook ya salió, no se pierde nada.")
        anotar("sin_camino")
        return None

    direccion = _direccion_de_la_foto(resultado, token, log=log)
    if not direccion:
        anotar("sin_direccion")
        return None

    try:
        # Paso 1: se arma el envase, que es Instagram yendo a buscar la imagen.
        r = requests.post(
            f"{GRAPH}/{ig}/media",
            data={"image_url": direccion, "caption": caption or "",
                  "access_token": token_ig(token)},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no aceptó la foto ({r.status_code}): "
                f"{(r.text or '')[:500]}")
            anotar("rechazada")
            return None
        envase = (r.json() or {}).get("id")
        if not envase:
            log("Instagram: no devolvió envase para publicar.")
            return None

        # Paso 2: se publica ese envase. Recién acá aparece en el perfil.
        r = requests.post(
            f"{GRAPH}/{ig}/media_publish",
            data={"creation_id": envase, "access_token": token_ig(token)},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no pudo publicar el envase ({r.status_code}): "
                f"{(r.text or '')[:500]}")
            return None
        ig_post = (r.json() or {}).get("id")
        log(f"Instagram: publicado {ig_post} en @{ficha.get('username')}.")
        anotar("apilada")
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

    propia = bool((os.environ.get("IG_TOKEN") or "").strip())
    lineas = []
    try:
        r = requests.get(
            f"{GRAPH}/{page_id}",
            params={"fields": "name,instagram_business_account{id,username,followers_count}",
                    "access_token": token_ig(token)},
            timeout=30,
        )
        datos = r.json() if r.content else {}
    except Exception as e:
        return f"❌ No pude preguntarle nada a Facebook: {e}"

    if r.status_code >= 400:
        error = ((datos.get("error") or {}).get("message") or "")[:300]
        lineas.append(f"❌ Facebook rechazó la consulta ({r.status_code}).")
        lineas.append(f"🔑 Llave usada: {'IG_TOKEN (aparte)' if propia else 'la de la página'}")
        if error:
            lineas.append(f"Dice: {error}")
        lineas.append("")
        lineas.append("Le faltan permisos: instagram_basic e "
                      "instagram_content_publish. Generá una llave con esas dos "
                      "casillas marcadas y guardala como secreto IG_TOKEN, así "
                      "no hay que tocar la que hoy publica en Facebook.")
        return "\n".join(lineas)

    ficha = datos.get("instagram_business_account")
    lineas.append(f"📘 Página: {datos.get('name') or page_id}")
    lineas.append(f"🔑 Llave: {'IG_TOKEN (aparte)' if propia else 'la de la página'}")
    if not ficha:
        lineas.append("")
        lineas.append("❌ No veo ninguna cuenta de Instagram vinculada.")
        lineas.append("")
        if propia:
            lineas.append("La llave IG_TOKEN llega a la página pero no ve el "
                          "Instagram. Le faltan instagram_basic e "
                          "instagram_content_publish; hay que generarla de nuevo "
                          "marcando esas dos casillas.")
        else:
            lineas.append("Si en Business Suite la ves conectada, entonces es la "
                          "llave: a la de la página le faltan instagram_basic e "
                          "instagram_content_publish.")
            lineas.append("")
            lineas.append("No hace falta tocar la llave que hoy publica en "
                          "Facebook: generá una nueva con esos permisos y "
                          "guardala como secreto IG_TOKEN. Si algo sale mal, "
                          "Facebook sigue publicando igual.")
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
            params={"limit": 1, "access_token": token_ig(token)},
            timeout=30,
        )
        if r2.status_code >= 400:
            detalle = ((r2.json().get("error") or {}).get("message") or "")[:300]
            lineas.append("⚠️ La veo, pero la llave no llega a su contenido.")
            if detalle:
                lineas.append(f"Dice: {detalle}")
            lineas.append("")
            lineas.append("Falta el permiso instagram_content_publish.")
        else:
            lineas.append("✅ Todo en orden: puedo publicar en esa cuenta.")
    except Exception as e:
        lineas.append(f"⚠️ No pude comprobar el permiso de publicación: {e}")

    lineas.append(_recuento())
    return "\n".join(lineas)


def _recuento():
    """Cómo vienen saliendo, en números. Contesta cuántos entran apilados."""
    try:
        estado = json.loads(CUENTA_PATH.read_text())
    except Exception:
        return "\n📊 Todavía no hay ninguno para contar."
    total = int(estado.get("total", 0))
    if not total:
        return "\n📊 Todavía no hay ninguno para contar."
    apilada = int(estado.get("apilada", 0))
    carrusel = int(estado.get("carrusel", 0))
    salieron = apilada + carrusel
    partes = [f"\n📊 De {total} intentos, salieron {salieron}:",
              f"   • {apilada} como imagen apilada ({apilada * 100 // total}%)",
              f"   • {carrusel} como carrusel ({carrusel * 100 // total}%)"]
    fallados = total - salieron
    if fallados:
        detalle = ", ".join(f"{v} {k.replace('_', ' ')}"
                            for k, v in sorted(estado.items())
                            if k not in ("total", "apilada", "carrusel"))
        partes.append(f"   • {fallados} no salieron ({detalle})")
    return "\n".join(partes)
