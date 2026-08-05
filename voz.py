#!/usr/bin/env python3
"""Voz en off para los reels, contra la API de ai33.pro.

Qué hace: le manda el guion al servicio, se trae el mp3 y —si el servicio lo
devuelve— también el momento exacto en que se dice cada palabra, que es lo que
deja los subtítulos calzados al milímetro.

La voz se elige por NOMBRE, no por id. El id de una voz clonada es un número
que no dice nada y que cambia si se vuelve a clonar; el nombre («VEXVIP») es lo
que se ve en el panel y lo que uno recuerda. Así que este módulo busca la voz
por nombre en la biblioteca, se queda con el id y lo guarda en disco para no
volver a preguntar en cada publicación.

Configuración (todo por variables de entorno, nada escrito en el código):

    VOZ_API_KEY     la clave del panel de ai33.pro          (obligatoria)
    VOZ_NOMBRE      nombre de la voz a usar                  (por defecto VEXVIP)
    VOZ_PROVEEDOR   de qué familia es la voz                 (por defecto clone)
    VOZ_ID          id ya resuelto, con prefijo; si se pone, se salta la búsqueda
    VOZ_VELOCIDAD   0.5 a 1.5                                (por defecto 1.0)
    VOZ_API_BASE    por si alguna vez cambia el dominio

Uso:

    import voz
    r = voz.sintetizar("El texto que se narra", "/tmp/voz.mp3")
    r["archivo"]   -> ruta del mp3
    r["segundos"]  -> cuánto dura
    r["marcas"]    -> [(palabra, inicio, fin)] o None si el servicio no las dio
"""
import os
import json
import time
import subprocess
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CARPETA_ESTADO = BASE_DIR / "state"
CACHE_VOCES = CARPETA_ESTADO / "voces.json"

API_BASE = os.environ.get("VOZ_API_BASE", "https://api.ai33.pro/v3").rstrip("/")
NOMBRE_POR_DEFECTO = os.environ.get("VOZ_NOMBRE", "VEXVIP")
VELOCIDAD_POR_DEFECTO = os.environ.get("VOZ_VELOCIDAD", "1.0")

# Las voces clonadas se piden con este prefijo, según la documentación.
PREFIJO_CLON = "clone_"
# Familias de voces del servicio. El id que devuelve la biblioteca ya viene con
# el prefijo puesto (por ejemplo "edge_vi-VN-HoaiMyNeural"), así que si un id
# trae uno se respeta tal cual.
PROVEEDORES = (
    "clone", "elevenlabs", "minimax", "edge", "kokoro", "vbee", "fishaudio",
)
PREFIJOS = tuple(p + "_" for p in PROVEEDORES)

# La voz que nos interesa es una clonada, así que se busca ahí primero; si no
# aparece, se barre el resto de familias antes de darse por vencido.
PROVEEDOR_POR_DEFECTO = os.environ.get("VOZ_PROVEEDOR", "clone").strip() or "clone"

# Ruta documentada del listado: GET /v3/voices?provider=...
RUTA_LISTADO = "/voices"
# El servicio no acepta páginas más grandes que esto.
TAMANO_MAX_PAGINA = 100

TIEMPO_LIMITE = 180


class ErrorDeVoz(RuntimeError):
    """No se pudo generar la voz en off."""


def log(msg):
    print(f"[voz] {msg}", flush=True)


def _clave():
    clave = (os.environ.get("VOZ_API_KEY") or "").strip()
    if not clave:
        raise ErrorDeVoz(
            "Falta VOZ_API_KEY. Se configura como secreto, nunca en el código."
        )
    return clave


def _cabeceras():
    return {"xi-api-key": _clave()}


def hay_voz():
    """Dice si el módulo está configurado, para poder saltarlo sin romper nada."""
    return bool((os.environ.get("VOZ_API_KEY") or "").strip())


# ---------------------------------------------------------------------------
# Encontrar la voz por su nombre
# ---------------------------------------------------------------------------

def _con_prefijo(identificador, proveedor=None):
    """Devuelve el id listo para pedirle audio al servicio.

    La biblioteca ya entrega los ids con su prefijo puesto, y la documentación
    dice de usarlos tal cual. Solo se agrega uno cuando el id viene pelado —el
    caso de un id copiado a mano del panel, o el que devuelve la clonación—, y
    ahí se usa la familia de la que salió; si no se sabe, se asume clonada.
    """
    texto = str(identificador).strip()
    if texto.startswith(PREFIJOS):
        return texto
    familia = (proveedor or "").strip().lower()
    prefijo = familia + "_" if familia in PROVEEDORES else PREFIJO_CLON
    return prefijo + texto


def _leer_cache():
    try:
        return json.loads(CACHE_VOCES.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _guardar_cache(datos):
    try:
        CARPETA_ESTADO.mkdir(parents=True, exist_ok=True)
        CACHE_VOCES.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log(f"No se pudo guardar el caché de voces ({e}); sigo igual.")


def _sacar_lista(datos):
    """Saca la lista de voces de la respuesta, venga con la envoltura que venga.

    Unos servicios devuelven {"data": {"voices": [...]}}, otros {"data": [...]},
    otros la lista pelada. En vez de adivinar, se busca la primera lista de
    diccionarios que tenga pinta de voces.
    """
    if isinstance(datos, list):
        return [d for d in datos if isinstance(d, dict)]
    if not isinstance(datos, dict):
        return []
    for clave in ("voices", "items", "list", "records", "results", "data"):
        if clave in datos:
            encontrada = _sacar_lista(datos[clave])
            if encontrada:
                return encontrada
    for valor in datos.values():
        if isinstance(valor, (list, dict)):
            encontrada = _sacar_lista(valor)
            if encontrada:
                return encontrada
    return []


def _nombre_de(voz):
    for clave in ("voice_name", "name", "title", "display_name", "nombre"):
        if voz.get(clave):
            return str(voz[clave])
    return ""


def _id_de(voz):
    for clave in ("voice_id", "id", "voiceId", "uuid"):
        if voz.get(clave) not in (None, ""):
            return str(voz[clave])
    return ""


def _hay_mas(datos):
    """Lee de la respuesta si quedan páginas por delante."""
    if not isinstance(datos, dict):
        return False
    pag = datos.get("pagination")
    if isinstance(pag, dict):
        return bool(pag.get("has_more"))
    return False


def listar_voces(proveedor=None, pagina=1, tamano=TAMANO_MAX_PAGINA, busqueda=None):
    """Trae una página de la biblioteca de voces.

    Devuelve (lista_de_voces, hay_mas). El parámetro `provider` es obligatorio
    para el servicio, así que siempre se manda uno.
    """
    familia = (proveedor or PROVEEDOR_POR_DEFECTO).strip().lower()
    parametros = {
        "provider": familia,
        "page": int(pagina),
        "page_size": min(int(tamano), TAMANO_MAX_PAGINA),
    }
    if busqueda:
        parametros["search"] = busqueda

    url = API_BASE + RUTA_LISTADO
    try:
        r = requests.get(url, headers=_cabeceras(), params=parametros, timeout=60)
    except Exception as e:
        raise ErrorDeVoz(f"No se pudo consultar la biblioteca de voces: {e}")
    if r.status_code >= 400:
        raise ErrorDeVoz(
            f"La biblioteca de voces respondió {r.status_code}: {r.text[:200]}"
        )
    try:
        datos = r.json()
    except Exception as e:
        raise ErrorDeVoz(f"La biblioteca de voces devolvió algo ilegible: {e}")
    return _sacar_lista(datos), _hay_mas(datos)


def _calza(voz, buscado):
    """Compara el nombre de una voz con lo buscado, sin mayúsculas ni espacios."""
    return _nombre_de(voz).lower().replace(" ", "") == buscado


def _buscar_en_proveedor(familia, nombre, buscado):
    """Busca la voz dentro de una familia. Devuelve el id pelado o None.

    Primero pregunta usando el buscador del servicio, que en general la
    encuentra de una. Si por lo que sea no la devuelve —el buscador mira varios
    campos y puede ordenar distinto—, recorre la biblioteca página por página.
    """
    try:
        voces, _ = listar_voces(familia, busqueda=nombre)
    except ErrorDeVoz as e:
        log(f"No pude buscar en «{familia}»: {e}")
        return None
    for v in voces:
        if _calza(v, buscado) and _id_de(v):
            return _id_de(v)

    pagina = 1
    while pagina <= 20:
        try:
            voces, hay_mas = listar_voces(familia, pagina=pagina)
        except ErrorDeVoz as e:
            log(f"No pude listar «{familia}» página {pagina}: {e}")
            return None
        for v in voces:
            if _calza(v, buscado) and _id_de(v):
                return _id_de(v)
        if not voces or not hay_mas:
            return None
        pagina += 1
    return None


def buscar_voz(nombre=None, refrescar=False, proveedor=None):
    """Devuelve el id (ya con prefijo) de la voz que se llama así.

    Primero mira el caché, después la variable de entorno, y recién ahí sale a
    preguntarle al servicio: empieza por la familia que corresponde —la de las
    voces clonadas— y, si no aparece, barre las demás antes de rendirse.
    """
    nombre = (nombre or NOMBRE_POR_DEFECTO).strip()

    directo = (os.environ.get("VOZ_ID") or "").strip()
    if directo and not refrescar:
        return _con_prefijo(directo, proveedor)

    cache = _leer_cache()
    clave_cache = nombre.lower()
    if not refrescar and cache.get(clave_cache):
        return cache[clave_cache]

    buscado = nombre.lower().replace(" ", "")
    primera = (proveedor or PROVEEDOR_POR_DEFECTO).strip().lower()
    orden = [primera] + [p for p in PROVEEDORES if p != primera]

    for familia in orden:
        identificador = _buscar_en_proveedor(familia, nombre, buscado)
        if identificador:
            final = _con_prefijo(identificador, familia)
            cache[clave_cache] = final
            _guardar_cache(cache)
            log(f"Voz «{nombre}» encontrada en la familia «{familia}» -> {final}")
            return final

    raise ErrorDeVoz(
        f"No hay ninguna voz llamada «{nombre}» en la biblioteca. "
        f"Revisá el nombre en el panel, o poné VOZ_ID a mano."
    )


# ---------------------------------------------------------------------------
# Generar el audio
# ---------------------------------------------------------------------------

def _buscar_url_audio(datos):
    """Rebusca en la respuesta hasta dar con el enlace del audio."""
    pistas = ("audio_url", "url", "audio", "file_url", "download_url", "mp3_url")
    if isinstance(datos, dict):
        for clave in pistas:
            valor = datos.get(clave)
            if isinstance(valor, str) and valor.startswith("http"):
                return valor
        for valor in datos.values():
            encontrado = _buscar_url_audio(valor)
            if encontrado:
                return encontrado
    elif isinstance(datos, list):
        for valor in datos:
            encontrado = _buscar_url_audio(valor)
            if encontrado:
                return encontrado
    elif isinstance(datos, str) and datos.startswith("http") and (
        ".mp3" in datos or ".wav" in datos or ".m4a" in datos
    ):
        return datos
    return None


def _buscar_marcas(datos):
    """Rebusca los tiempos por palabra dentro de la respuesta.

    Se acepta cualquier lista de objetos que traiga un texto y un principio y un
    final, sin importar cómo se llamen los campos ni si vienen en segundos o en
    milisegundos.
    """
    claves_texto = ("word", "text", "char", "token", "value", "palabra")
    claves_inicio = ("start", "start_time", "begin", "from", "startTime", "offset")
    claves_fin = ("end", "end_time", "finish", "to", "endTime")

    def parece(lista):
        if not isinstance(lista, list) or not lista:
            return False
        primero = lista[0]
        if not isinstance(primero, dict):
            return False
        tiene_texto = any(k in primero for k in claves_texto)
        tiene_inicio = any(k in primero for k in claves_inicio)
        return tiene_texto and tiene_inicio

    def convertir(lista):
        salida, escala = [], 1.0
        # Si los números son grandes, están en milisegundos. Se miran tanto los
        # comienzos como los finales: el comienzo de la primera palabra siempre
        # es chico y por sí solo no dice nada.
        muestras = []
        for item in lista:
            for k in claves_inicio + claves_fin:
                if isinstance(item.get(k), (int, float)):
                    muestras.append(float(item[k]))
        if muestras and max(muestras) > 1000:
            escala = 0.001
        for item in lista:
            palabra = next(
                (str(item[k]) for k in claves_texto if item.get(k) not in (None, "")), ""
            )
            inicio = next(
                (float(item[k]) for k in claves_inicio
                 if isinstance(item.get(k), (int, float))), None
            )
            fin = next(
                (float(item[k]) for k in claves_fin
                 if isinstance(item.get(k), (int, float))), None
            )
            if not palabra.strip() or inicio is None:
                continue
            inicio *= escala
            fin = fin * escala if fin is not None else inicio + 0.25
            salida.append((palabra.strip(), inicio, fin))
        return salida

    if parece(datos):
        return convertir(datos)
    if isinstance(datos, dict):
        for valor in datos.values():
            encontrado = _buscar_marcas(valor)
            if encontrado:
                return encontrado
    elif isinstance(datos, list):
        for valor in datos:
            encontrado = _buscar_marcas(valor)
            if encontrado:
                return encontrado
    return None


def _juntar_por_palabra(marcas):
    """Si el servicio devuelve los tiempos letra por letra, los agrupa.

    Algunos servicios devuelven una marca por carácter. Para el subtítulo eso no
    sirve: hay que rearmar las palabras, tomando el principio de la primera letra
    y el final de la última.
    """
    if not marcas:
        return marcas
    if sum(1 for p, _, _ in marcas if len(p) > 1) > len(marcas) * 0.4:
        return marcas  # ya vienen por palabra
    palabras, actual, inicio, fin = [], "", None, None
    for texto, a, b in marcas:
        if texto.isspace():
            continue
        if actual and inicio is not None:
            actual += texto
            fin = b
        else:
            actual, inicio, fin = texto, a, b
        if texto.endswith((" ",)) or len(actual) > 24:
            palabras.append((actual.strip(), inicio, fin))
            actual, inicio, fin = "", None, None
    if actual:
        palabras.append((actual.strip(), inicio, fin))
    return [(p, a, b) for p, a, b in palabras if p]


def _ajustar_a_duracion(marcas, segundos):
    """Última red de seguridad para las unidades de tiempo.

    Ya con el audio en la mano se sabe cuánto dura de verdad. Si las marcas se
    pasan de largo por un factor enorme, es que venían en milisegundos y la
    heurística no lo cazó; se dividen y listo.
    """
    if not marcas or segundos <= 0:
        return marcas
    ultimo = max(b for _, _, b in marcas)
    if ultimo > segundos * 3:
        factor = 0.001 if ultimo / 1000 <= segundos * 1.6 else segundos / ultimo
        log(f"Los tiempos venían en otra unidad; los reescalo por {factor}.")
        return [(p, a * factor, b * factor) for p, a, b in marcas]
    return marcas


def duracion(ruta):
    try:
        salida = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(ruta)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(salida)
    except Exception:
        return 0.0


def sintetizar(texto, salida, voz=None, velocidad=None, con_marcas=True,
               intentos=3):
    """Genera la voz en off y devuelve la ficha del audio.

    texto      -- el guion a narrar
    salida     -- dónde dejar el mp3
    voz        -- nombre o id de la voz; si no se pasa, va la de por defecto
    velocidad  -- 0.5 a 1.5
    con_marcas -- pedirle al servicio los tiempos por palabra
    """
    texto = (texto or "").strip()
    if not texto:
        raise ErrorDeVoz("No hay texto que narrar.")

    pedido = voz or NOMBRE_POR_DEFECTO
    # Si ya viene con prefijo del servicio, es un id; si no, es un nombre.
    identificador = pedido if str(pedido).startswith(PREFIJOS) else buscar_voz(pedido)

    campos = {
        "text": (None, texto),
        "voice_id": (None, identificador),
        "speed": (None, str(velocidad or VELOCIDAD_POR_DEFECTO)),
        "with_transcript": (None, "true" if con_marcas else "false"),
    }

    url = f"{API_BASE}/text-to-speech"
    datos = None
    ultimo = None
    for intento in range(1, intentos + 1):
        try:
            r = requests.post(url, headers=_cabeceras(), files=campos,
                              timeout=TIEMPO_LIMITE)
            if r.status_code >= 400:
                ultimo = f"{r.status_code} {r.text[:200]}"
            else:
                datos = r.json()
                break
        except Exception as e:
            ultimo = str(e)
        if intento < intentos:
            espera = 4 * intento
            log(f"Reintento {intento} en {espera}s ({ultimo}).")
            time.sleep(espera)
    if datos is None:
        raise ErrorDeVoz(f"El servicio de voz no respondió bien: {ultimo}")

    enlace = _buscar_url_audio(datos)
    if not enlace:
        raise ErrorDeVoz(
            "La respuesta no trae enlace de audio: " + json.dumps(datos)[:300]
        )

    salida = Path(salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(enlace, timeout=TIEMPO_LIMITE, stream=True) as descarga:
        descarga.raise_for_status()
        with open(salida, "wb") as f:
            for pedazo in descarga.iter_content(65536):
                f.write(pedazo)

    segundos = duracion(salida)
    marcas = _juntar_por_palabra(_buscar_marcas(datos)) if con_marcas else None
    marcas = _ajustar_a_duracion(marcas, segundos)
    log(
        f"Voz lista: {salida.name} ({segundos:.1f} s, "
        f"{'con' if marcas else 'sin'} tiempos por palabra)."
    )
    return {
        "archivo": str(salida),
        "segundos": segundos,
        "marcas": marcas,
        "voz": identificador,
    }


def cuanto_texto_entra(segundos, palabras_por_minuto=165):
    """Cuántas palabras entran en ese tiempo, para acotar el guion.

    165 palabras por minuto es un ritmo de locución normal en español; con eso
    un reel de 28 segundos admite unas 77 palabras.
    """
    return int(segundos * palabras_por_minuto / 60)


if __name__ == "__main__":
    import sys

    if not hay_voz():
        print("Falta VOZ_API_KEY en el entorno.")
        raise SystemExit(2)

    # `python3 voz.py voces [familia]` lista lo que hay en la biblioteca, que es
    # la forma rápida de comprobar desde el servidor que la clave sirve y que la
    # voz está donde creemos.
    if len(sys.argv) > 1 and sys.argv[1] == "voces":
        familia = sys.argv[2] if len(sys.argv) > 2 else PROVEEDOR_POR_DEFECTO
        voces, hay_mas = listar_voces(familia)
        print(f"Familia «{familia}»: {len(voces)} voces" + (" (hay más)" if hay_mas else ""))
        for v in voces:
            print(f"  {_nombre_de(v)}  ->  {_id_de(v)}")
        raise SystemExit(0)

    texto = sys.argv[1] if len(sys.argv) > 1 else "Probando la voz del bot."
    destino = sys.argv[2] if len(sys.argv) > 2 else "/tmp/voz_prueba.mp3"
    ficha = sintetizar(texto, destino)
    print(json.dumps({k: v for k, v in ficha.items() if k != "marcas"}, indent=2))
    if ficha["marcas"]:
        print(f"{len(ficha['marcas'])} palabras con tiempo propio.")
