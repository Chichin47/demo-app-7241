#!/usr/bin/env python3
"""Reconocimiento de participantes: quién es quién en cada foto.

No se "entrena" ningún modelo. Se usan dos modelos ya hechos de OpenCV:

  * YuNet  -> encuentra las caras en una foto (y 5 puntos: ojos, nariz, boca).
  * SFace  -> convierte cada cara en una "huella" de 128 números. Dos fotos de
              la misma persona dan huellas parecidas; de personas distintas, no.

Registrar a alguien (por Telegram, `/participante Pascal` + fotos) es guardar
varias huellas suyas con su nombre. Reconocer es sacar la huella de cada cara
de una foto nueva y ver a qué nombre se parece más.

Las huellas son datos biométricos y el repositorio es público: por eso se
guardan CIFRADAS dentro de state/rostros.json (que a su vez viaja empaquetado
en data/store.b64, como el resto del estado). La llave sale de ROSTROS_KEY si
existe; si no, se deriva del token del bot de Telegram, que ya es secreto. Si
un día se cambia ese token, las huellas viejas no se pueden abrir y hay que
volver a registrar a los participantes (el bot lo avisa).

Los modelos (~40 MB entre los dos) no van en el repositorio: se bajan la
primera vez a modelos_rostros/ y el workflow los guarda en caché. Si YuNet no
estuviera, se usa un detector de respaldo que trae OpenCV (menos preciso), así
nada se cae.
"""
import base64
import hashlib
import json
import os
import time
import unicodedata
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
ESTADO_PATH = BASE_DIR / "state" / "rostros.json"
MODELOS_DIR = Path(os.environ.get("ROSTROS_MODELOS") or (BASE_DIR / "modelos_rostros"))
FUENTE = BASE_DIR / "fonts" / "Anton-Regular.ttf"

YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"
# Los modelos viven en Git LFS: media.githubusercontent.com da el archivo
# real; github.com/.../raw redirige ahí (queda de segunda opción).
_ZOOS = ["https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models",
         "https://github.com/opencv/opencv_zoo/raw/main/models"]
URLS = {
    YUNET: [f"{z}/face_detection_yunet/{YUNET}" for z in _ZOOS],
    SFACE: [f"{z}/face_recognition_sface/{SFACE}" for z in _ZOOS],
}
# Huella de los archivos oficiales: si lo bajado no coincide, no se usa.
SHA256 = {
    YUNET: "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    SFACE: "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
}
# Tamaño mínimo razonable de cada archivo: si baja menos, es una página de
# error y no el modelo.
TAM_MIN = {YUNET: 100_000, SFACE: 30_000_000}

# Similitud (coseno) a partir de la cual SFace da dos caras por la misma
# persona. 0.363 es el valor que recomienda OpenCV para este modelo.
UMBRAL = 0.363
# Por encima de esto lo damos por seguro (lo de en medio es "probable").
SEGURO = 0.50
# El mejor candidato tiene que ganarle al segundo por al menos esto; si no,
# es que la cara se parece a dos personas y no conviene adivinar.
MARGEN = 0.04
# Huellas por persona. Más no mejora mucho y agranda el estado.
MAX_HUELLAS = 20
# Una huella casi idéntica a otra ya guardada no suma nada (foto repetida).
DUPLICADO = 0.97
# Caras más chicas que esto (lado, en px) se reconocen mal.
CARA_MIN_PX = 40
CARA_CHICA_PX = 70
# Las fotos se achican a esto (lado mayor) solo para detectar, por velocidad.
LADO_DETECCION = 1280


def log(msg):
    print(f"[rostros] {msg}", flush=True)


# --------------------------------------------------------------------------
# Nombres
# --------------------------------------------------------------------------

def clave_nombre(nombre):
    """'Pascál  ' -> 'pascal'. Así Pascal, PASCAL y pascal son la misma persona."""
    t = unicodedata.normalize("NFD", (nombre or "").strip().lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = "".join(c if (c.isalnum() or c.isspace()) else " " for c in t)
    return " ".join(t.split())


def nombre_bonito(nombre):
    limpio = " ".join((nombre or "").strip().split())
    return " ".join(p[:1].upper() + p[1:].lower() for p in limpio.split(" "))


# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------

def _ruta_modelo(nombre):
    return MODELOS_DIR / nombre


def _modelo_ok(nombre):
    ruta = _ruta_modelo(nombre)
    return ruta.exists() and ruta.stat().st_size >= TAM_MIN[nombre]


def asegurar_modelos():
    """Baja los modelos que falten. Devuelve {nombre: True/False}."""
    import requests
    MODELOS_DIR.mkdir(parents=True, exist_ok=True)
    estado = {}
    for nombre, urls in URLS.items():
        if _modelo_ok(nombre):
            estado[nombre] = True
            continue
        estado[nombre] = False
        tmp = _ruta_modelo(nombre).with_suffix(".part")
        for url in urls:
            try:
                with requests.get(url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as fh:
                        for trozo in r.iter_content(1 << 20):
                            fh.write(trozo)
                h = hashlib.sha256(tmp.read_bytes()).hexdigest()
                if h != SHA256[nombre]:
                    raise RuntimeError(f"el archivo bajado no es el oficial "
                                       f"({tmp.stat().st_size} bytes, sha256 {h[:12]}…)")
                tmp.replace(_ruta_modelo(nombre))
                log(f"Modelo {nombre} descargado y verificado.")
                estado[nombre] = True
                break
            except Exception as e:  # noqa: BLE001
                log(f"No se pudo bajar {nombre} de {url.split('/')[2]}: {e}")
                tmp.unlink(missing_ok=True)
    return estado


_cache = {}


def _cv():
    import cv2
    return cv2


def usa_yunet():
    return _modelo_ok(YUNET)


def _reconocedor():
    if "rec" not in _cache:
        if not _modelo_ok(SFACE):
            asegurar_modelos()
        if not _modelo_ok(SFACE):
            raise RuntimeError("falta el modelo de reconocimiento (SFace); "
                               "no se pudo descargar")
        _cache["rec"] = _cv().FaceRecognizerSF.create(str(_ruta_modelo(SFACE)), "")
    return _cache["rec"]


def _detector_yunet(w, h):
    cv2 = _cv()
    if "yunet" not in _cache:
        _cache["yunet"] = cv2.FaceDetectorYN.create(
            str(_ruta_modelo(YUNET)), "", (w, h), 0.7, 0.3, 5000)
    det = _cache["yunet"]
    det.setInputSize((w, h))
    return det


# --------------------------------------------------------------------------
# Imagen, detección y huellas
# --------------------------------------------------------------------------

def leer_imagen(ruta):
    cv2 = _cv()
    datos = np.fromfile(str(ruta), dtype=np.uint8)
    img = cv2.imdecode(datos, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"no se pudo abrir la imagen {Path(ruta).name}")
    return img


def _fila_desde_caja(x, y, w, h, score=0.9):
    """Arma una fila con el formato de YuNet a partir de una caja suelta,
    estimando dónde caen ojos, nariz y boca. Solo para el detector de
    respaldo: SFace necesita esos puntos para enderezar la cara."""
    pts = [
        (x + 0.30 * w, y + 0.38 * h),  # ojo derecho (izquierda de la foto)
        (x + 0.70 * w, y + 0.38 * h),  # ojo izquierdo
        (x + 0.50 * w, y + 0.58 * h),  # nariz
        (x + 0.35 * w, y + 0.78 * h),  # comisura derecha
        (x + 0.65 * w, y + 0.78 * h),  # comisura izquierda
    ]
    fila = [x, y, w, h] + [c for p in pts for c in p] + [score]
    return np.array(fila, dtype=np.float32)


def detectar(img):
    """Devuelve las caras de la foto (filas de 15 valores, formato YuNet, en
    coordenadas de la foto original), de la más grande a la más chica."""
    cv2 = _cv()
    alto, ancho = img.shape[:2]
    escala = min(1.0, LADO_DETECCION / max(alto, ancho))
    chica = img if escala == 1.0 else cv2.resize(
        img, (int(ancho * escala), int(alto * escala)), interpolation=cv2.INTER_AREA)
    ch, cw = chica.shape[:2]

    filas = []
    if usa_yunet():
        _, caras = _detector_yunet(cw, ch).detect(chica)
        if caras is not None:
            filas = [np.array(c, dtype=np.float32) for c in caras]
    else:
        gris = cv2.cvtColor(chica, cv2.COLOR_BGR2GRAY)
        cascada = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        cajas = cascada.detectMultiScale(gris, 1.1, 6, minSize=(24, 24))
        filas = [_fila_desde_caja(*map(float, c)) for c in cajas]

    salida = []
    for f in filas:
        f = f.copy()
        f[:14] /= escala  # de vuelta a la foto original
        if min(f[2], f[3]) >= CARA_MIN_PX:
            salida.append(f)
    salida.sort(key=lambda f: -(f[2] * f[3]))
    return salida


def huella(img, fila):
    rec = _reconocedor()
    alineada = rec.alignCrop(img, fila)
    v = rec.feature(alineada).flatten().astype(np.float32)
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


def _cod(v):
    return base64.b64encode(np.asarray(v, dtype=np.float16).tobytes()).decode("ascii")


def _dec(texto):
    v = np.frombuffer(base64.b64decode(texto), dtype=np.float16).astype(np.float32)
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


# --------------------------------------------------------------------------
# Almacenamiento cifrado
# --------------------------------------------------------------------------

def _llave():
    origen = (os.environ.get("ROSTROS_KEY") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not origen:
        raise RuntimeError("no hay llave para cifrar las huellas "
                           "(falta ROSTROS_KEY o TELEGRAM_BOT_TOKEN)")
    return base64.urlsafe_b64encode(hashlib.sha256(("rostros-v1|" + origen).encode()).digest())


def _id_llave(llave):
    return hashlib.sha256(llave).hexdigest()[:10]


def _vacia():
    return {"personas": {}}


def cargar():
    """Devuelve la base {"personas": {clave: {...}}}. Nunca lanza por datos
    rotos: en ese caso arranca vacía y lo deja anotado en `_aviso`."""
    if not ESTADO_PATH.exists():
        return _vacia()
    try:
        crudo = json.loads(ESTADO_PATH.read_text(encoding="utf-8"))
        llave = _llave()
        if crudo.get("llave") and crudo["llave"] != _id_llave(llave):
            db = _vacia()
            db["_aviso"] = ("las huellas guardadas se cifraron con otra llave "
                            "(¿cambió el token del bot?); hay que volver a "
                            "registrar a los participantes")
            return db
        from cryptography.fernet import Fernet
        datos = Fernet(llave).decrypt(crudo["datos"].encode("ascii"))
        db = json.loads(datos.decode("utf-8"))
        db.setdefault("personas", {})
        return db
    except Exception as e:  # noqa: BLE001
        log(f"No se pudieron leer las huellas guardadas: {e}")
        db = _vacia()
        db["_aviso"] = f"no se pudieron leer las huellas guardadas ({e})"
        return db


def guardar(db):
    from cryptography.fernet import Fernet
    llave = _llave()
    limpio = {k: v for k, v in db.items() if not k.startswith("_")}
    token = Fernet(llave).encrypt(json.dumps(limpio, ensure_ascii=False).encode("utf-8"))
    ESTADO_PATH.parent.mkdir(parents=True, exist_ok=True)
    ESTADO_PATH.write_text(json.dumps({
        "v": 1,
        "actualizado": time.time(),
        "llave": _id_llave(llave),
        "datos": token.decode("ascii"),
    }), encoding="utf-8")


def _matrices(db):
    """{clave: matriz (n, 128)} con las huellas de cada persona."""
    salida = {}
    for clave, p in db.get("personas", {}).items():
        hs = [_dec(h["f"]) for h in p.get("huellas", [])]
        if hs:
            salida[clave] = np.stack(hs)
    return salida


def _puntaje(vec, matriz):
    """Qué tanto se parece una cara a una persona: el promedio de sus 2
    huellas más parecidas (una sola foto muy parecida por casualidad no
    alcanza; tampoco se castiga a quien tiene fotos de perfil)."""
    sims = matriz @ vec
    top = np.sort(sims)[-2:]
    return float(top.mean())


# --------------------------------------------------------------------------
# Operaciones
# --------------------------------------------------------------------------

def listar():
    db = cargar()
    personas = sorted(db["personas"].values(), key=lambda p: clave_nombre(p["nombre"]))
    return [{"nombre": p["nombre"], "huellas": len(p.get("huellas", [])),
             "creado": p.get("creado", 0)} for p in personas], db.get("_aviso")


def olvidar(nombre):
    db = cargar()
    clave = clave_nombre(nombre)
    if clave not in db["personas"]:
        return None
    p = db["personas"].pop(clave)
    guardar(db)
    return p["nombre"]


def _recorte(img, fila, lado=180):
    """Recorte cuadrado de la cara con un poco de aire, para las miniaturas."""
    cv2 = _cv()
    x, y, w, h = [float(v) for v in fila[:4]]
    cx, cy = x + w / 2, y + h / 2
    m = max(w, h) * 1.5
    alto, ancho = img.shape[:2]
    x0, y0 = int(max(0, cx - m / 2)), int(max(0, cy - m / 2))
    x1, y1 = int(min(ancho, cx + m / 2)), int(min(alto, cy + m / 2))
    trozo = img[y0:y1, x0:x1]
    if trozo.size == 0:
        trozo = img
    return cv2.resize(trozo, (lado, lado), interpolation=cv2.INTER_AREA)


def registrar(nombre, rutas):
    """Guarda huellas de `nombre` a partir de sus fotos.

    En cada foto se toma la cara más grande. Si hay varias caras de tamaño
    parecido y la persona ya tiene huellas, se toma la que más se le parece.
    Devuelve un resumen con lo guardado, lo que no sirvió y avisos.
    """
    db = cargar()
    clave = clave_nombre(nombre)
    if not clave:
        raise ValueError("falta el nombre")
    persona = db["personas"].setdefault(clave, {
        "nombre": nombre_bonito(nombre), "huellas": [], "creado": time.time()})
    matrices = _matrices(db)
    propias = matrices.get(clave)

    res = {"nombre": persona["nombre"], "guardadas": 0, "sin_cara": [],
           "repetidas": [], "avisos": [], "recortes": [], "aviso_base": db.get("_aviso")}
    for i, ruta in enumerate(rutas, 1):
        try:
            img = leer_imagen(ruta)
        except Exception as e:  # noqa: BLE001
            res["avisos"].append(f"foto {i}: no se pudo abrir ({e})")
            continue
        filas = detectar(img)
        if not filas:
            res["sin_cara"].append(i)
            continue

        mayor = filas[0][2] * filas[0][3]
        candidatas = [f for f in filas if f[2] * f[3] >= 0.35 * mayor]
        if len(candidatas) > 1 and propias is not None:
            con_huella = [(f, huella(img, f)) for f in candidatas]
            fila, vec = max(con_huella, key=lambda t: _puntaje(t[1], propias))
        else:
            fila = filas[0]
            vec = huella(img, fila)
            if len(candidatas) > 1:
                res["avisos"].append(
                    f"foto {i}: había {len(candidatas)} caras; usé la más grande "
                    "(mejor mandar fotos donde se le vea solo a él/ella)")

        if propias is not None and float((propias @ vec).max()) >= DUPLICADO:
            res["repetidas"].append(i)
            continue

        lado = int(min(fila[2], fila[3]))
        if lado < CARA_CHICA_PX:
            res["avisos"].append(f"foto {i}: la cara sale chica ({lado} px); "
                                 "sirve, pero una captura más cercana ayuda")

        # ¿Se parece más a otra persona ya registrada?
        mejor_otro, sim_otro = None, -1.0
        for otra, m in matrices.items():
            if otra == clave:
                continue
            s = _puntaje(vec, m)
            if s > sim_otro:
                mejor_otro, sim_otro = otra, s
        propia = _puntaje(vec, propias) if propias is not None else -1.0
        if mejor_otro and sim_otro >= UMBRAL and sim_otro > propia:
            otro = db["personas"][mejor_otro]["nombre"]
            res["avisos"].append(f"foto {i}: esta cara se parece más a {otro}; "
                                 "la guardé igual como "
                                 f"{persona['nombre']}. Si fue un error, "
                                 f"/olvidar {persona['nombre']} y volvé a registrarlo")

        persona["huellas"].append({"f": _cod(vec), "t": time.time()})
        propias = vec[None, :] if propias is None else np.vstack([propias, vec])
        res["guardadas"] += 1
        res["recortes"].append(_recorte(img, fila))

    if len(persona["huellas"]) > MAX_HUELLAS:
        persona["huellas"] = persona["huellas"][-MAX_HUELLAS:]
    if not persona["huellas"]:
        db["personas"].pop(clave, None)  # no quedó ninguna cara útil
    if res["guardadas"]:
        guardar(db)
    res["total"] = len(persona["huellas"])
    return res


def agregar_huella(nombre, vec):
    """Suma una huella ya calculada (corrección desde el chat: "2 es Pascal")."""
    db = cargar()
    clave = clave_nombre(nombre)
    persona = db["personas"].setdefault(clave, {
        "nombre": nombre_bonito(nombre), "huellas": [], "creado": time.time()})
    persona["huellas"].append({"f": _cod(vec), "t": time.time()})
    persona["huellas"] = persona["huellas"][-MAX_HUELLAS:]
    guardar(db)
    return persona["nombre"], len(persona["huellas"])


def reconocer(img, db=None):
    """Pone nombre a cada cara de la foto.

    Devuelve una lista (de la cara más grande a la más chica) de dicts:
      fila, caja (x, y, w, h), huella, nombre (o None), confianza
      ("seguro" | "probable" | None), puntaje, candidato (mejor parecido
      aunque no alcance el umbral) y puntaje_candidato.
    Una misma persona no se asigna a dos caras de la misma foto.
    """
    db = db if db is not None else cargar()
    matrices = _matrices(db)
    filas = detectar(img)
    caras = []
    for f in filas:
        vec = huella(img, f)
        puntajes = sorted(((_puntaje(vec, m), k) for k, m in matrices.items()), reverse=True)
        caras.append({"fila": f, "caja": tuple(int(v) for v in f[:4]), "huella": vec,
                      "puntajes": puntajes, "nombre": None, "confianza": None,
                      "puntaje": 0.0,
                      "candidato": db["personas"][puntajes[0][1]]["nombre"] if puntajes else None,
                      "puntaje_candidato": puntajes[0][0] if puntajes else 0.0})

    # Asignación de a pares, de la coincidencia más fuerte a la más débil.
    pares = []
    for i, c in enumerate(caras):
        for pos, (s, k) in enumerate(c["puntajes"]):
            segundo = c["puntajes"][pos + 1][0] if pos + 1 < len(c["puntajes"]) else -1.0
            pares.append((s, i, k, s - segundo if pos == 0 else 0.0, pos))
    pares.sort(reverse=True)
    usadas, tomadas = set(), set()
    for s, i, k, margen, pos in pares:
        if i in usadas or k in tomadas:
            continue
        c = caras[i]
        # Este es el mejor nombre todavía libre para esta cara. Si no alcanza,
        # o si la cara se parece casi igual a dos personas, queda sin nombre
        # (no se prueba con el siguiente: sería adivinar).
        if s < UMBRAL or (pos == 0 and margen < MARGEN):
            usadas.add(i)
            continue
        c["nombre"] = db["personas"][k]["nombre"]
        c["puntaje"] = s
        c["confianza"] = "seguro" if s >= SEGURO else "probable"
        usadas.add(i)
        tomadas.add(k)
    for c in caras:
        c.pop("puntajes", None)
    return caras


# --------------------------------------------------------------------------
# Imágenes para el chat
# --------------------------------------------------------------------------

def _pil(img_bgr):
    from PIL import Image
    return Image.fromarray(img_bgr[:, :, ::-1].copy())


def _fuente(tam):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(FUENTE), tam)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def anotar(img, caras, salida, inicio=1):
    """Foto con un recuadro numerado por cara y el nombre reconocido."""
    from PIL import ImageDraw
    im = _pil(img)
    escala = max(im.width, im.height) / 1280
    if escala > 1:
        im = im.resize((int(im.width / escala), int(im.height / escala)))
    else:
        escala = 1.0
    d = ImageDraw.Draw(im)
    grosor = max(3, im.width // 300)
    tam = max(22, im.width // 38)
    fuente = _fuente(tam)
    for n, c in enumerate(caras, inicio):
        x, y, w, h = [v / escala for v in c["caja"]]
        if c["nombre"]:
            color = "#FFD400" if c["confianza"] == "seguro" else "#FF9F1A"
            texto = f"{n} · {c['nombre'].upper()}" + ("" if c["confianza"] == "seguro" else " ?")
        else:
            color = "#BBBBBB"
            texto = f"{n} · ?"
        d.rectangle([x, y, x + w, y + h], outline=color, width=grosor)
        tw = d.textlength(texto, font=fuente)
        ty = y - tam - 12 if y - tam - 12 > 0 else y + h + 4
        d.rectangle([x, ty, x + tw + 16, ty + tam + 10], fill="#000000")
        d.text((x + 8, ty + 3), texto, font=fuente, fill=color)
    im.convert("RGB").save(salida, "JPEG", quality=90)
    return salida


def hoja_contactos(recortes, salida, columnas=5):
    """Miniaturas de las caras guardadas, para confirmar el registro."""
    from PIL import Image
    if not recortes:
        return None
    lado, gap = 180, 8
    filas = (len(recortes) + columnas - 1) // columnas
    cols = min(columnas, len(recortes))
    hoja = Image.new("RGB", (cols * (lado + gap) + gap, filas * (lado + gap) + gap), "#000000")
    for i, r in enumerate(recortes):
        hoja.paste(_pil(r), (gap + (i % columnas) * (lado + gap), gap + (i // columnas) * (lado + gap)))
    hoja.save(salida, "JPEG", quality=90)
    return salida


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "modelos":
        est = asegurar_modelos()
        print(json.dumps(est))
        # Que no alcance con bajarlos: que esta versión de OpenCV los abra.
        try:
            prueba = np.zeros((320, 320, 3), dtype=np.uint8)
            detectar(prueba)
            _reconocedor()
            print(f"Modelos listos (detector: {'YuNet' if usa_yunet() else 'respaldo'}).")
            # Prueba con una foto de muestra de OpenCV: solo informativa.
            try:
                import requests
                r = requests.get("https://raw.githubusercontent.com/opencv/opencv/"
                                 "4.x/samples/data/lena.jpg", timeout=30)
                muestra = _cv().imdecode(np.frombuffer(r.content, np.uint8), 1)
                caras = detectar(muestra)
                v = huella(muestra, caras[0]) if caras else None
                print(f"Foto de muestra: {len(caras)} cara(s) detectada(s)"
                      + (f", huella de {v.size} valores." if v is not None else "."))
            except Exception as e:  # noqa: BLE001
                print(f"(No se pudo hacer la prueba con la foto de muestra: {e})")
        except Exception as e:  # noqa: BLE001
            print(f"Los modelos no se pudieron abrir: {e}")
            sys.exit(1)
        sys.exit(0 if all(est.values()) else 1)
    print("Uso: python rostros.py modelos")
