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
import time
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com/v25.0"

# Puerta por la que Instagram acepta el archivo SUBIDO, en vez de ir a buscarlo
# a una dirección. Es la misma versión que GRAPH, sacada de ahí para que no se
# desincronicen si un día se cambia una sola.
SUBIDA = f"https://rupload.facebook.com/ig-api-upload/{GRAPH.rsplit('/', 1)[-1]}"

CUENTA_PATH = Path(__file__).resolve().parent / "state" / "instagram.json"

# Un carrusel de Instagram admite de 2 a 10 diapositivas.
CARRUSEL_MAXIMO = 10

# Cuánto se espera a cada llamada. Instagram tiene que ir a buscar la imagen a
# la dirección que le pasamos, así que la primera es la más lenta de las dos.
ESPERA = 60

# Cuánto se le aguanta a Instagram procesando un video antes de dar por perdido
# el intento. Con un reel de menos de un minuto suele tardar entre 30 y 60
# segundos; cuatro minutos es de sobra y evita que un cuelgue frene el ciclo.
ESPERA_VIDEO = 360

# Las fotos se procesan en segundos, pero "segundos" no es "al instante": hay
# que preguntar igual antes de publicar, o Instagram devuelve el error 9007.
ESPERA_FOTO = 90

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

    El orden es: USER_TOKEN si está, después IG_TOKEN, y como último recurso la
    de la página por si ya tuviera los permisos.

    USER_TOKEN va primero desde que el bot se fabrica solo las llaves de las
    páginas: es la misma llave de usuario larga que usa para eso, y ya trae
    instagram_basic e instagram_content_publish. Así queda UNA sola llave que
    renovar cada 60 días en vez de tres, que es de donde salían los líos.
    IG_TOKEN se deja funcionando para no romper lo que ya estaba puesto.
    """
    return ((os.environ.get("USER_TOKEN") or "").strip()
            or (os.environ.get("IG_TOKEN") or "").strip()
            or token_pagina)


def cuenta(page_id, token, log=print):
    """El identificador de la cuenta de Instagram vinculada a esta página.

    Devuelve None si no hay ninguna vinculada, o si el token no alcanza para
    verla (que es el caso más probable la primera vez: hay que regenerarlo con
    los permisos instagram_basic e instagram_content_publish).

    Con IG_PAGINA en el entorno se busca la cuenta vinculada a ESA página en
    vez de a la que publica. Existe porque la página que publica puede cambiar
    y el Instagram quedarse: hoy @universorealityvip está vinculado a Mexico
    Lives aunque quien publica en Facebook sea otra página.
    """
    forzada = (os.environ.get("IG_PAGINA") or "").strip()
    if forzada:
        page_id = forzada
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
            # Sin esta espera, Instagram contesta 9007 al publicar: el envase
            # existe pero él todavía no terminó de bajar la foto.
            if not _esperar_envase(hijo, token_ig(token), log=log,
                                   espera=ESPERA_FOTO,
                                   que_es=f"la diapositiva {n}"):
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
        if not _esperar_envase(envase, token_ig(token), log=log,
                               espera=ESPERA_FOTO, que_es="el carrusel"):
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


def _direccion_del_video(video_id, page_id, token, ruta, log=print):
    """La dirección pública del reel, para que Instagram lo vaya a buscar.

    OJO CON EL ORDEN, que acá estuvo el error. Al principio se usaba la
    dirección del reel ya publicado en la página, porque salía gratis. Con las
    fotos eso funciona: la dirección que da Facebook es un .jpg de verdad, un
    archivo suelto que cualquiera puede bajar. Con los reels no: Facebook los
    sirve en streaming, y esa dirección no es un mp4 que Instagram pueda
    descargarse de un saque. Por eso las fotos entraban y los videos se
    quedaban colgados sin terminar nunca.

    Así que ahora primero se sube una copia SIN PUBLICAR del mismo archivo: esa
    copia sí devuelve el mp4 original, tal cual, y es lo que Instagram necesita.
    La copia no sale en la página, no la ve nadie, y se borra apenas termina.
    La dirección del reel publicado queda como último recurso.

    Devuelve (direccion, id_temporal_para_borrar).
    """
    try:
        with open(ruta, "rb") as f:
            r = requests.post(
                f"{GRAPH}/{page_id}/videos",
                files={"source": f},
                data={"published": "false", "access_token": token},
                timeout=600,
            )
        if r.status_code >= 400:
            log(f"Instagram: no pude dejar la copia del video en Facebook "
                f"({r.status_code}): {(r.text or '')[:300]}")
            return _ultimo_recurso(video_id, token, log=log), None
        copia = (r.json() or {}).get("id")
        if not copia:
            return _ultimo_recurso(video_id, token, log=log), None
        # La copia recién subida tarda un momento en tener dirección.
        for _ in range(12):
            r2 = requests.get(f"{GRAPH}/{copia}",
                              params={"fields": "source", "access_token": token},
                              timeout=30)
            fuente = (r2.json() or {}).get("source") if r2.status_code < 400 else None
            if fuente:
                return fuente, copia
            time.sleep(5)
        log("Instagram: la copia del video nunca tuvo dirección.")
        return _ultimo_recurso(video_id, token, log=log), copia
    except Exception as e:
        log(f"Instagram: falló al preparar el video ({e}).")
        return _ultimo_recurso(video_id, token, log=log), None


def _ultimo_recurso(video_id, token, log=print):
    """La dirección del reel ya publicado. Se usa solo si la copia no salió.

    Se deja como red de seguridad y no como camino principal porque es
    justamente la que venía fallando: Facebook sirve los reels en streaming y
    esa dirección no siempre es un archivo que Instagram pueda bajarse.
    """
    if not video_id:
        return None
    try:
        r = requests.get(f"{GRAPH}/{video_id}",
                         params={"fields": "source", "access_token": token},
                         timeout=30)
        if r.status_code < 400:
            fuente = (r.json() or {}).get("source")
            if fuente:
                log("Instagram: uso la dirección del reel publicado como "
                    "último recurso; puede que no la acepte.")
                return fuente
    except Exception:
        pass
    return None


def _esperar_envase(envase, token, log=print, espera=None, que_es="el video"):
    """Espera a que Instagram termine de procesar lo que le mandamos.

    Esto NO es opcional y hay que hacerlo con TODO, no solo con el video. Meta
    contesta el envase al instante, pero por dentro todavía está bajando y
    procesando el archivo. Si se publica antes de tiempo devuelve el error 9007
    ("Media ID is not available" / "el archivo multimedia no está listo para
    publicar; espera un momento"), que es exactamente lo que estaba pasando con
    los carruseles.

    Se pregunta cada 10 segundos y se deja escrito el estado que va viendo, para
    que la próxima vez no haya que adivinar si se colgó o si lo rechazó.
    """
    espera = espera if espera is not None else ESPERA_VIDEO
    ultimo = None
    for vuelta in range(max(1, espera // 10)):
        try:
            r = requests.get(f"{GRAPH}/{envase}",
                             params={"fields": "status_code,status",
                                     "access_token": token},
                             timeout=30)
            datos = r.json() if r.content else {}
            estado = datos.get("status_code")
            if estado != ultimo:
                log(f"Instagram: {que_es} va en {estado}.")
                ultimo = estado
            if estado == "FINISHED":
                return True
            if estado == "ERROR":
                log(f"Instagram: rechazó {que_es}: {str(datos.get('status'))[:400]}")
                return False
        except Exception as e:
            log(f"Instagram: no pude preguntar cómo va {que_es} ({e}).")
        time.sleep(10)
    log(f"Instagram: {que_es} sigue en {ultimo} después de {espera // 60} "
        f"minutos; lo dejo. En Facebook ya salió igual.")
    return False


def _envase_subiendo_el_archivo(ig, token, ruta, caption, log=print):
    """Le manda el mp4 a Instagram directamente, sin pasar por ninguna dirección.

    Este es el camino bueno y es el que Meta documenta para archivos propios.
    Se pide el envase con upload_type=resumable —que no lleva video_url— y el
    archivo se sube a mano a rupload.facebook.com. Instagram no tiene que ir a
    buscar nada a ningún lado: le llegan los bytes y listo.

    Antes se hacía al revés: se le pasaba la dirección de una copia del video
    guardada en Facebook, y Facebook no siempre devuelve un mp4 que se pueda
    bajar de un saque (los sirve en streaming). De ahí venían los videos que
    quedaban en IN_PROGRESS para siempre, y después el ERROR 2207076.

    Devuelve el identificador del envase, o None si algo no salió.
    """
    if not ruta or not Path(ruta).exists():
        return None
    try:
        r = requests.post(
            f"{GRAPH}/{ig}/media",
            data={"media_type": "REELS", "upload_type": "resumable",
                  "caption": caption or "", "share_to_feed": "true",
                  "access_token": token},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no me dio el envase para subir el video "
                f"({r.status_code}): {(r.text or '')[:300]}")
            return None
        envase = (r.json() or {}).get("id")
        if not envase:
            return None

        tamano = Path(ruta).stat().st_size
        with open(ruta, "rb") as f:
            r2 = requests.post(
                f"{SUBIDA}/{envase}",
                headers={"Authorization": f"OAuth {token}",
                         "offset": "0",
                         "file_size": str(tamano)},
                data=f,
                timeout=600,
            )
        if r2.status_code >= 400:
            log(f"Instagram: falló la subida del archivo ({r2.status_code}): "
                f"{(r2.text or '')[:300]}")
            return None
        log(f"Instagram: video subido entero ({tamano / 1048576:.1f} MB); "
            f"ahora lo procesa.")
        return envase
    except Exception as e:
        log(f"Instagram: no pude subir el archivo del video ({e}).")
        return None


def publicar_reel(page_id, token, video_id, caption, ruta, log=print):
    """Manda a Instagram el mismo reel que acaba de salir en la página 2.

    Es el mismo archivo, ya renderizado: no se arma nada de nuevo ni se le pide
    nada a Claude. Lo único distinto con las fotos es que Instagram necesita su
    tiempo para bajarlo y procesarlo, así que hay que esperarlo.

    Devuelve el identificador del post de Instagram, o None; nunca revienta.
    """
    if not activo():
        return None
    ficha = cuenta(page_id, token, log=log)
    if not ficha:
        return None
    ig = ficha.get("id")

    temporal = None
    try:
        # Camino principal: el archivo se le manda a Instagram, punto. Es el
        # que Meta documenta para videos propios y el que no depende de que
        # Facebook sepa servir el mp4.
        envase = _envase_subiendo_el_archivo(ig, token_ig(token), ruta, caption,
                                             log=log)

        # Red de seguridad: el camino de antes, pasándole una dirección. Queda
        # por si un día la subida directa no está disponible, pero es el que
        # venía fallando, así que va segundo y no primero.
        if not envase:
            log("Instagram: no salió la subida directa; pruebo pasándole la "
                "dirección del video.")
            direccion, temporal = _direccion_del_video(video_id, page_id, token,
                                                       ruta, log=log)
            if not direccion:
                anotar("reel_sin_direccion")
                return None
            r = requests.post(
                f"{GRAPH}/{ig}/media",
                data={"media_type": "REELS", "video_url": direccion,
                      "caption": caption or "", "share_to_feed": "true",
                      "access_token": token_ig(token)},
                timeout=ESPERA,
            )
            if r.status_code >= 400:
                log(f"Instagram: no aceptó el video ({r.status_code}): "
                    f"{(r.text or '')[:500]}")
                anotar("reel_rechazado")
                return None
            envase = (r.json() or {}).get("id")
        if not envase:
            anotar("reel_sin_direccion")
            return None

        if not _esperar_envase(envase, token_ig(token), log=log):
            anotar("reel_sin_terminar")
            return None

        r = requests.post(
            f"{GRAPH}/{ig}/media_publish",
            data={"creation_id": envase, "access_token": token_ig(token)},
            timeout=ESPERA,
        )
        if r.status_code >= 400:
            log(f"Instagram: no pudo publicar el video ({r.status_code}): "
                f"{(r.text or '')[:400]}")
            anotar("reel_fallado")
            return None
        ig_post = (r.json() or {}).get("id")
        log(f"Instagram: publicado {ig_post} como reel.")
        anotar("reel")
        return ig_post
    except Exception as e:
        log(f"Instagram: falló el video ({e}).")
        anotar("reel_fallado")
        return None
    finally:
        if temporal:
            _borrar_ocultas([temporal], token, log=log)


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

    # De dónde sale la dirección de la imagen. Lo normal es del post que acaba
    # de salir en Facebook (resultado). Pero si no hay post de Facebook —por
    # ejemplo un encargo de "solo Instagram"— se sube una copia OCULTA a la
    # página solo para que Instagram tenga de dónde bajarla, y se borra al
    # final, igual que se hace con las diapositivas del carrusel.
    temporal = None
    if resultado:
        direccion = _direccion_de_la_foto(resultado, token, log=log)
    elif ruta:
        direccion, temporal = _subir_oculta(page_id, token, ruta, log=log)
    else:
        direccion = None
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
        if not _esperar_envase(envase, token_ig(token), log=log,
                               espera=ESPERA_FOTO, que_es="la foto"):
            anotar("foto_sin_terminar")
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
    finally:
        if temporal:
            _borrar_ocultas([temporal], token, log=log)


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

    dias = vencimiento(token_ig(token))
    if dias is None:
        pass
    elif dias == float("inf"):
        lineas.append("♾️ La llave no vence.")
    elif dias <= 0:
        lineas.append("⛔ La llave YA VENCIÓ: hay que renovarla.")
    elif dias <= 7:
        lineas.append(f"⚠️ A la llave le quedan {dias:.0f} días. Conviene renovarla ya.")
    else:
        lineas.append(f"⏳ A la llave le quedan {dias:.0f} días.")

    lineas.append(_recuento())
    return "\n".join(lineas)


def vencimiento(token):
    """Cuántos días le quedan a la llave de Instagram. None si no se puede saber.

    Mira DOS relojes distintos, y este es el punto:

    * expires_at: cuándo caduca la llave. En cero significa que no caduca.
    * data_access_expires_at: la ventana de acceso a datos que Meta le pone
      aparte. Vence sola cada tantos meses aunque la llave sea eterna, y
      cuando vence la llave deja de servir igual.

    Mirar solo el primero es la trampa: la llave dice "no vence nunca" y un
    día deja de funcionar igual. Así que se devuelve el más cercano de los dos.
    """
    try:
        r = requests.get(
            f"{GRAPH}/debug_token",
            params={"input_token": token, "access_token": token},
            timeout=30,
        )
        if r.status_code >= 400:
            return None
        datos = ((r.json() or {}).get("data") or {})
    except Exception:
        return None

    ahora = time.time()
    plazos = []
    for campo in ("expires_at", "data_access_expires_at"):
        valor = datos.get(campo)
        if valor is None:
            continue
        if valor == 0:          # cero = ese reloj no corre
            continue
        plazos.append((valor - ahora) / 86400.0)
    if not plazos:
        # Ninguno de los dos relojes corre: la llave es realmente eterna.
        return float("inf") if datos else None
    return min(plazos)


def aviso_de_vencimiento(token, log=print):
    """Deja escrito en el registro cuánto le queda, y grita si queda poco."""
    dias = vencimiento(token)
    if dias is None:
        return None
    if dias == float("inf"):
        log("Instagram: la llave no vence.")
        return None
    if dias <= 0:
        log("Instagram: la llave YA VENCIÓ. Hay que renovarla.")
    elif dias <= 7:
        log(f"Instagram: OJO, a la llave le quedan {dias:.0f} días. Renovala.")
    else:
        log(f"Instagram: a la llave le quedan {dias:.0f} días.")
    return dias


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
    reel = int(estado.get("reel", 0))
    salieron = apilada + carrusel + reel
    partes = [f"\n📊 De {total} intentos, salieron {salieron}:",
              f"   • {apilada} como imagen apilada ({apilada * 100 // total}%)",
              f"   • {carrusel} como carrusel ({carrusel * 100 // total}%)"]
    if reel:
        partes.append(f"   • {reel} como video ({reel * 100 // total}%)")
    fallados = total - salieron
    if fallados:
        detalle = ", ".join(f"{v} {k.replace('_', ' ')}"
                            for k, v in sorted(estado.items())
                            if k not in ("total", "apilada", "carrusel", "reel"))
        partes.append(f"   • {fallados} no salieron ({detalle})")
    return "\n".join(partes)
