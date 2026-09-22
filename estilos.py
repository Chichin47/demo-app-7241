#!/usr/bin/env python3
"""Dibuja los 5 estilos de post (ver docs/Fichas Tecnicas Estilos.md).

Recibe un PLAN ya decidido (lo arma diseno.py) y las fotos, y devuelve un PNG
de 1080x1350. No decide nada de contenido: qué estilo, qué textos y qué foto
va en cada lugar ya vienen en el plan. Sí decide lo que depende de cómo quedan
las fotos una vez puestas, y lo anota en el plan para que volver a dibujar dé
exactamente lo mismo:

  * el par de colores del texto (según el fondo que queda detrás de la frase,
    para que la palabra resaltada no se pierda con la ropa o el fondo),
  * la posición del círculo en el 1a (lejos de las caras).

Estilos:  1a Detalle + Reacciones · 2a Duelo vertical · 3a Clásico renovado
          4a Mosaico · 5a Bloque con círculos
"""
import colorsys
import math
import random
import re
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance

BASE_DIR = Path(__file__).resolve().parent
ANTON = BASE_DIR / "fonts" / "Anton-Regular.ttf"
OSWALD = BASE_DIR / "fonts" / "Oswald.ttf"
EMOJI_FUENTES = [
    BASE_DIR / "fonts" / "NotoColorEmoji.ttf",
    Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
    Path("/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf"),
]

W, H = 1080, 1350
SEGURO = 60  # margen de la zona segura

NOMBRES = {
    "1a": "Detalle + Reacciones",
    "2a": "Duelo vertical",
    "3a": "Clásico renovado",
    "4a": "Mosaico",
    "5a": "Bloque con círculos",
}

# 7.1 Paletas de acento: (acento 1, acento 2)
PALETAS = {
    "rojo_amarillo": ("#E4002B", "#FFD400"),
    "azul_blanco": ("#0B5FFF", "#FFFFFF"),
    "violeta_rosa": ("#8B2FC9", "#FF4FA3"),
    "verde_lima": ("#00A859", "#C6FF00"),
    "naranja_negro": ("#FF6B00", "#111111"),
}
PALETA_POR_TONO = {
    "pelea": ["rojo_amarillo", "naranja_negro"],
    "sorpresa": ["rojo_amarillo", "azul_blanco", "naranja_negro"],
    "tristeza": ["azul_blanco", "violeta_rosa"],
    "triunfo": ["verde_lima", "rojo_amarillo"],
    "intriga": ["violeta_rosa", "azul_blanco"],
    "verguenza": ["violeta_rosa", "naranja_negro"],
    "chisme": ["violeta_rosa", "rojo_amarillo"],
}

# 7.1b Pares de color de texto: (base, resaltado, contorno)
PARES = {
    "blanco_amarillo": ("#FFFFFF", "#FFD400", "#000000"),
    "blanco_rojo": ("#FFFFFF", "#FF2D3D", "#000000"),
    "amarillo_rojo": ("#FFD400", "#FF2D3D", "#000000"),
    "amarillo_blanco": ("#FFD400", "#FFFFFF", "#000000"),
    "blanco_cian": ("#FFFFFF", "#00E5FF", "#000000"),
    "blanco_rosa": ("#FFFFFF", "#FF4FA3", "#000000"),
}
PAR_POR_TONO = {
    "pelea": ["blanco_amarillo", "blanco_rojo", "amarillo_rojo"],
    "sorpresa": ["blanco_amarillo", "amarillo_blanco", "blanco_cian"],
    "tristeza": ["blanco_cian", "blanco_amarillo", "blanco_rosa"],
    "triunfo": ["blanco_amarillo", "amarillo_blanco", "blanco_cian"],
    "intriga": ["blanco_rosa", "blanco_cian", "blanco_amarillo"],
    "verguenza": ["blanco_rosa", "blanco_amarillo"],
    "chisme": ["blanco_rosa", "blanco_amarillo", "amarillo_rojo"],
}

EMOJIS_POR_TONO = {
    "pelea": ["😡", "🔥", "💥", "👊"],
    "sorpresa": ["😱", "😳", "👀", "🤯"],
    "tristeza": ["🥺", "💔", "😢", "😔"],
    "triunfo": ["🎉", "✅", "😍", "🙌"],
    "intriga": ["🤔", "👀", "❓", "🕵️"],
    "verguenza": ["😬", "🙈", "😅"],
    "chisme": ["👀", "🔥", "😳", "💬"],
}

RE_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿←-⇿️‍]+")


# --------------------------------------------------------------------------
# Utilidades de color
# --------------------------------------------------------------------------

def _rgb(hexa):
    h = hexa.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    def c(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b)


def _contraste(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hsv(rgb):
    return colorsys.rgb_to_hsv(*(v / 255 for v in rgb))


def analizar_zona(img):
    """Color promedio y matices dominantes (en grados) de una zona."""
    chica = img.convert("RGB").resize((48, 24))
    px = list(chica.getdata())
    prom = tuple(int(sum(p[i] for p in px) / len(px)) for i in range(3))
    bins = [0.0] * 12
    total = 0.0
    for p in px:
        h, s, v = _hsv(p)
        if s > 0.35 and v > 0.25:
            peso = s * v
            bins[int(h * 12) % 12] += peso
            total += peso
    dominantes = []
    if total > 0:
        for i, b in enumerate(bins):
            # matiz relevante si ocupa buena parte de lo que tiene color
            if b / total > 0.18 and total / len(px) > 0.06:
                dominantes.append(i * 30 + 15)
    return prom, dominantes


def _dist_matiz(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def elegir_par(zona, tono, rng, preferido=None, evitar=None):
    """Elige el par base/resaltado que se lea sobre `zona` (7.1b)."""
    prom, dominantes = analizar_zona(zona)
    orden = list(PAR_POR_TONO.get(tono) or []) + [p for p in PARES if p not in (PAR_POR_TONO.get(tono) or [])]
    if preferido in PARES:
        orden.remove(preferido)
        orden.insert(0, preferido)
    buenos = []
    for pid in orden:
        base, resalt, _ = PARES[pid]
        h, s, _v = _hsv(_rgb(resalt))
        if s > 0.3 and any(_dist_matiz(h * 360, d) < 35 for d in dominantes):
            continue  # el resaltado se pierde con la ropa o el fondo
        if _contraste(_rgb(base), prom) < 3.0:
            continue
        buenos.append(pid)
    if not buenos:
        return "blanco_amarillo"
    if preferido in buenos:
        return preferido
    candidatos = [p for p in buenos if p != evitar] or buenos
    # Los primeros (los del tono) pesan más, pero hay variedad.
    pesos = [max(1, 6 - i) for i in range(len(candidatos))]
    return rng.choices(candidatos, weights=pesos, k=1)[0]


# --------------------------------------------------------------------------
# Fuentes, emojis y texto
# --------------------------------------------------------------------------

_fuentes = {}


def _fuente(ruta, tam, variacion=None):
    clave = (str(ruta), tam, variacion)
    if clave not in _fuentes:
        f = ImageFont.truetype(str(ruta), tam)
        if variacion:
            try:
                f.set_variation_by_name(variacion)
            except Exception:  # noqa: BLE001
                pass
        _fuentes[clave] = f
    return _fuentes[clave]


_fuente_emoji = []


def _emoji_img(texto, alto):
    """El emoji dibujado a color, del alto pedido. None si no hay fuente."""
    if not texto:
        return None
    if not _fuente_emoji:
        f = None
        for r in EMOJI_FUENTES:
            if r.exists():
                try:
                    f = ImageFont.truetype(str(r), 109)
                    break
                except Exception:  # noqa: BLE001
                    continue
        _fuente_emoji.append(f)
    f = _fuente_emoji[0]
    if f is None:
        return None
    try:
        lienzo = Image.new("RGBA", (140 * max(1, len(texto)), 150), (0, 0, 0, 0))
        ImageDraw.Draw(lienzo).text((6, 6), texto, font=f, embedded_color=True)
        caja = lienzo.getbbox()
        if not caja:
            return None
        lienzo = lienzo.crop(caja)
        esc = alto / lienzo.height
        return lienzo.resize((max(1, int(lienzo.width * esc)), int(alto)), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        return None


def _cuantos_emojis(texto):
    return max(1, sum(1 for ch in texto or "" if ord(ch) > 0x2000 and ch not in "\ufe0f\u200d"
                      and not (0x1F3FB <= ord(ch) <= 0x1F3FF)))


def separar_emojis(texto):
    """('FRASE SIN EMOJIS', '😡💥')"""
    emojis = "".join(RE_EMOJI.findall(texto or ""))
    limpio = " ".join(RE_EMOJI.sub(" ", texto or "").split())
    return limpio, emojis


def _norm(p):
    t = unicodedata.normalize("NFD", p.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9ñ]", "", t)


def _tokens(frase, resaltado):
    """Palabras de la frase con la marca de si van resaltadas."""
    palabras = frase.upper().split()
    marcas = [False] * len(palabras)
    obj = [_norm(p) for p in (resaltado or "").split() if _norm(p)]
    if obj:
        norm = [_norm(p) for p in palabras]
        hallado = False
        for i in range(len(norm) - len(obj) + 1):
            if norm[i:i + len(obj)] == obj:
                for j in range(i, i + len(obj)):
                    marcas[j] = True
                hallado = True
                break
        if not hallado:
            for i, n in enumerate(norm):
                if n and n in obj:
                    marcas[i] = True
    return list(zip(palabras, marcas))


def _medir(tokens, fuente, cap, tracking=0, *tokens_emojis):
    esp = fuente.getlength(" ")
    anchos = []
    for t, _m in tokens:
        if t == "\x00EMOJI":
            anchos.append(cap * (0.15 + 1.2 * _cuantos_emojis(tokens_emojis[0] if tokens_emojis else "")))
        else:
            anchos.append(fuente.getlength(t) + tracking * max(0, len(t) - 1))
    return anchos, esp


def _partir(tokens, anchos, esp, ancho_max):
    """Reparte las palabras en renglones. Cada renglón: ([(palabra, marca, ancho)], ancho)."""
    lineas, actual, w = [], [], 0.0
    for (tok, marca), a in zip(tokens, anchos):
        nuevo = a if not actual else w + esp + a
        # El emoji nunca queda solo en un renglón: va pegado a la última palabra.
        if actual and nuevo > ancho_max and tok != "\x00EMOJI":
            lineas.append((actual, w))
            actual, w = [(tok, marca, a)], a
        else:
            actual.append((tok, marca, a))
            w = nuevo
    if actual:
        lineas.append((actual, w))
    return lineas


def texto_rico(lienzo, frase, resaltado, emojis, x, ancho, y, *, tam_max, tam_min,
               max_lineas, colores, ancla="arriba", fuente_ruta=ANTON, variacion=None,
               contorno=0, interlinea=1.22, tracking=0, sombra=False, alinear="centro"):
    """Dibuja una frase con palabras resaltadas y emojis al final.

    `ancla`: 'arriba' (y = borde de arriba), 'abajo' (y = borde de abajo) o
    'centro'. Achica la letra hasta que entre en `max_lineas`. Devuelve la
    caja (x0, y0, x1, y1) de lo dibujado.
    """
    base, resalt, trazo = colores
    tokens = _tokens(frase, resaltado)
    if emojis:
        tokens.append(("\x00EMOJI", False))
    if not tokens:
        return (x, y, x, y)
    tam = tam_max
    while True:
        f = _fuente(fuente_ruta, tam, variacion)
        cap = -f.getbbox("H", anchor="ls")[1]
        anchos, esp = _medir(tokens, f, cap, tracking, emojis)
        lineas = _partir(tokens, anchos, esp, ancho)
        if (len(lineas) <= max_lineas and all(w <= ancho for _, w in lineas)) or tam <= tam_min:
            break
        tam -= 4
    paso = cap * interlinea
    alto = cap + paso * (len(lineas) - 1)
    if ancla == "abajo":
        y0 = y - alto
    elif ancla == "centro":
        y0 = y - alto / 2
    else:
        y0 = y
    d = ImageDraw.Draw(lienzo)
    x_min, x_max = 1e9, -1e9
    for n, (linea, w) in enumerate(lineas):
        base_y = y0 + cap + paso * n
        cx = x + (ancho - w) / 2 if alinear == "centro" else x
        x_min, x_max = min(x_min, cx), max(x_max, cx + w)
        for tok, marca, a in linea:
            if tok == "\x00EMOJI":
                em = _emoji_img(emojis, cap * 1.15)
                if em is not None:
                    lienzo.paste(em, (int(cx + cap * 0.1), int(base_y - em.height + cap * 0.08)), em)
                cx += a + esp
                continue
            color = resalt if marca else base
            if sombra:
                d.text((cx + 4, base_y + 5), tok, font=f, fill=(0, 0, 0, 160), anchor="ls")
            if tracking:
                xx = cx
                for ch in tok:
                    d.text((xx, base_y), ch, font=f, fill=color, anchor="ls",
                           stroke_width=contorno, stroke_fill=trazo)
                    xx += f.getlength(ch) + tracking
            else:
                d.text((cx, base_y), tok, font=f, fill=color, anchor="ls",
                       stroke_width=contorno, stroke_fill=trazo)
            cx += a + esp
    return (x_min, y0, x_max, y0 + alto)


# --------------------------------------------------------------------------
# Fotos
# --------------------------------------------------------------------------

def abrir(ruta):
    img = Image.open(ruta)
    img.load()
    return img.convert("RGB")


def cubrir(img, w, h, cara=None, objetivo=0.0, zoom_max=2.2, alto_cara=0.38):
    """Llena un marco w x h con la foto (modo cover), centrando la cara.

    cara: [x, y, w, h] en la foto original. objetivo: qué fracción del alto
    del marco debería ocupar la cara (0 = no acercar). Devuelve (imagen,
    transformación) donde transformación = (escala, izquierda, arriba) para
    ubicar después las caras dentro del marco.
    """
    iw, ih = img.size
    s = max(w / iw, h / ih)
    if cara and cara[3] * s > 0.62 * h and w > h * 1.3:
        # Foto vertical en un marco apaisado: con "cover" la cara no entra.
        # Se muestra la foto entera de alto sobre un fondo de ella misma,
        # desenfocado y oscurecido.
        return _con_relleno(img, w, h, cara)
    if cara and objetivo:
        fh = max(1.0, cara[3])
        s = max(s, min(objetivo * h / fh, s * zoom_max))
    if cara:
        cx, cy = cara[0] + cara[2] / 2, cara[1] + cara[3] / 2
    else:
        cx, cy = iw / 2, ih / 2
    nw, nh = iw * s, ih * s
    izq = min(max(cx * s - w / 2, 0), nw - w)
    arr = min(max(cy * s - h * alto_cara, 0), nh - h)
    caja = (izq / s, arr / s, (izq + w) / s, (arr + h) / s)
    out = img.resize((w, h), Image.LANCZOS, box=caja)
    return out, (s, izq, arr)


def _con_relleno(img, w, h, cara):
    iw, ih = img.size
    s = min(h / ih, 0.62 * h / max(1.0, cara[3]) * 1.35)
    s = max(s, h * 0.9 / ih) if ih * s < h * 0.9 else s
    fw, fh = int(iw * s), int(ih * s)
    fondo, _ = cubrir(img, w, h, None)
    fondo = ImageEnhance.Brightness(fondo.filter(ImageFilter.GaussianBlur(22))).enhance(0.55)
    frente = img.resize((fw, fh), Image.LANCZOS)
    cx = (cara[0] + cara[2] / 2) * s
    x = int(min(max(w / 2 - cx, w - fw), 0)) if fw > w else int((w - fw) / 2)
    y = int((h - fh) / 2)
    fondo.paste(frente, (x, y))
    return fondo, (s, -x, -y)


def caras_en_marco(caras, trans, x0, y0, w, h):
    """Pasa las cajas de caras de la foto a coordenadas del lienzo."""
    s, izq, arr = trans
    salida = []
    for c in caras or []:
        x, y, cw, ch = c["caja"]
        nx, ny = x * s - izq + x0, y * s - arr + y0
        nw, nh = cw * s, ch * s
        if nx + nw > x0 and nx < x0 + w and ny + nh > y0 and ny < y0 + h:
            salida.append((nx, ny, nw, nh))
    return salida


def recorte_circular(img, cara, diam, ocupa=0.55):
    """La cara recortada en círculo, de `diam` px (RGBA)."""
    iw, ih = img.size
    if cara:
        cx, cy = cara[0] + cara[2] / 2, cara[1] + cara[3] / 2 + cara[3] * 0.05
        lado = max(cara[2], cara[3]) / ocupa
    else:
        cx, cy, lado = iw / 2, ih / 2, min(iw, ih) * 0.8
    lado = min(lado, iw, ih)
    x0 = min(max(cx - lado / 2, 0), iw - lado)
    y0 = min(max(cy - lado / 2, 0), ih - lado)
    foto = img.resize((diam, diam), Image.LANCZOS, box=(x0, y0, x0 + lado, y0 + lado)).convert("RGBA")
    mascara = Image.new("L", (diam * 3, diam * 3), 0)
    ImageDraw.Draw(mascara).ellipse((0, 0, diam * 3 - 1, diam * 3 - 1), fill=255)
    foto.putalpha(mascara.resize((diam, diam), Image.LANCZOS))
    return foto


def pegar_circulo(lienzo, foto, cx, cy, anillos, sombra=(0, 14, 40, 150)):
    """Pega la foto circular con anillos de colores (de adentro hacia afuera)."""
    diam = foto.width
    r_total = diam / 2 + sum(g for g, _ in anillos)
    lado = int(r_total * 2) + 2
    if sombra:
        dx, dy, blur, alfa = sombra
        capa = Image.new("RGBA", (lado + blur * 4, lado + blur * 4), (0, 0, 0, 0))
        m = blur * 2
        ImageDraw.Draw(capa).ellipse((m, m, m + lado, m + lado), fill=(0, 0, 0, alfa))
        capa = capa.filter(ImageFilter.GaussianBlur(blur / 2))
        lienzo.alpha_composite(capa, (int(cx - lado / 2 - m + dx), int(cy - lado / 2 - m + dy)))
    k = 3
    aro = Image.new("RGBA", (lado * k, lado * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(aro)
    r = r_total
    for g, color in reversed(anillos):
        c = lado * k / 2
        d.ellipse((c - r * k, c - r * k, c + r * k, c + r * k), fill=color)
        r -= g
    aro = aro.resize((lado, lado), Image.LANCZOS)
    lienzo.alpha_composite(aro, (int(cx - lado / 2), int(cy - lado / 2)))
    lienzo.alpha_composite(foto, (int(cx - diam / 2), int(cy - diam / 2)))


def degradado(w, h, paradas):
    """Degradado vertical RGBA. paradas: [(0..1, (r,g,b,a)), ...]"""
    col = Image.new("RGBA", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for (t0, c0), (t1, c1) in zip(paradas, paradas[1:]):
            if t0 <= t <= t1:
                u = (t - t0) / max(1e-6, t1 - t0)
                px[0, y] = tuple(int(c0[i] + (c1[i] - c0[i]) * u) for i in range(4))
                break
    return col.resize((w, h))


def _cara_mayor(foto):
    caras = foto.get("caras") or []
    return caras[0]["caja"] if caras else None


def _cara_de(foto, nombre):
    for c in foto.get("caras") or []:
        if nombre and c.get("nombre") and c["nombre"].lower() == nombre.lower():
            return c["caja"]
    return None


def _slot_cara(foto, slot):
    """La cara a centrar en un lugar: la de la persona pedida, o la más grande."""
    return (_cara_de(foto, slot.get("persona")) if slot.get("persona") else None) or \
        slot.get("cara") or _cara_mayor(foto)


# --------------------------------------------------------------------------
# Flecha (1a)
# --------------------------------------------------------------------------

def flecha(lienzo, p0, p1, curva, color="#E4002B", borde="#FFFFFF"):
    """Flecha curva de p0 a p1 (la punta en p1)."""
    k = 2
    capa = Image.new("RGBA", (W * k, H * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    (x0, y0), (x1, y1) = p0, p1
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    nx, ny = -(y1 - y0), (x1 - x0)
    n = math.hypot(nx, ny) or 1
    cx, cy = mx + nx / n * curva, my + ny / n * curva
    pts = []
    for i in range(41):
        t = i / 40
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1
        pts.append((x * k, y * k))
    ang = math.atan2(y1 - pts[-6][1] / k, x1 - pts[-6][0] / k)
    largo, abre = 70, 0.62
    punta = [(x1 * k, y1 * k),
             ((x1 - largo * math.cos(ang - abre)) * k, (y1 - largo * math.sin(ang - abre)) * k),
             ((x1 - largo * math.cos(ang + abre)) * k, (y1 - largo * math.sin(ang + abre)) * k)]
    cuerpo = pts[:-4]
    d.line(cuerpo, fill=borde, width=46 * k, joint="curve")
    d.polygon(punta, fill=borde, outline=borde, width=16 * k)
    d.line(cuerpo, fill=color, width=28 * k, joint="curve")
    d.polygon(punta, fill=color)
    lienzo.alpha_composite(capa.resize((W, H), Image.LANCZOS))


# --------------------------------------------------------------------------
# Estilos
# --------------------------------------------------------------------------

def _colores_texto(lienzo, zona, plan, rng):
    v = plan.setdefault("variante", {})
    if v.get("par") not in PARES:
        v["par"] = elegir_par(lienzo.crop(zona), plan.get("tono"), rng,
                              preferido=v.get("par_preferido"), evitar=v.get("evitar_par"))
    return PARES[v["par"]]


def _acentos(plan):
    v = plan.setdefault("variante", {})
    if v.get("paleta") not in PALETAS:
        v["paleta"] = "rojo_amarillo"
    return PALETAS[v["paleta"]]


def estilo_2a(fotos, plan, rng):
    v = plan["variante"]
    t = plan["textos"]
    fondo = "#FFFFFF" if v.get("fondo", "blanco") == "blanco" else "#000000"
    lienzo = Image.new("RGBA", (W, H), fondo)
    for lado, slot in zip((0, 544), plan["slots"]["fotos"][:2]):
        f = fotos[slot["i"]]
        img, _ = cubrir(f["img"], 536, H, _slot_cara(f, slot), objetivo=0.24, alto_cara=0.34)
        lienzo.paste(img, (lado, 0))
    lienzo.alpha_composite(degradado(W, 640, [(0, (0, 0, 0, 0)), (0.45, (0, 0, 0, 235)), (1, (0, 0, 0, 255))]), (0, 710))
    acento1, _ = _acentos(plan)
    colores = _colores_texto(lienzo, (60, 860, 1020, 1180), plan, rng)
    sub, sub_emo = separar_emojis(t.get("subtitulo") or "")
    tit, _ = separar_emojis(t.get("titular") or "")
    y_sub = 1236
    if sub:
        texto_rico(lienzo, sub, "", sub_emo, 50, 980, y_sub, tam_max=46, tam_min=30, max_lineas=1,
                   colores=(colores[0], colores[0], "#000000"), ancla="centro",
                   fuente_ruta=OSWALD, variacion=b"Bold")
    caja = texto_rico(lienzo, tit, t.get("resaltado"), "", 50, 980, 1180, tam_max=160, tam_min=90,
                      max_lineas=2, colores=colores, ancla="abajo", interlinea=1.12, tracking=2)
    ImageDraw.Draw(lienzo).rectangle((360, caja[1] - 42, 720, caja[1] - 32), fill=acento1)
    return lienzo


def estilo_3a(fotos, plan, rng):
    v = plan["variante"]
    fondo = "#FFFFFF" if v.get("fondo", "blanco") == "blanco" else "#000000"
    lienzo = Image.new("RGBA", (W, H), fondo)
    paneles = [(0, 0, 671), (1, 679, 671)]
    for (n, y0, h), slot in zip(paneles, plan["slots"]["fotos"][:2]):
        f = fotos[slot["i"]]
        img, _ = cubrir(f["img"], W, h, _slot_cara(f, slot), objetivo=0.30, alto_cara=0.33)
        lienzo.paste(img, (0, y0))
        lienzo.alpha_composite(degradado(W, 260, [(0, (0, 0, 0, 0)), (1, (0, 0, 0, 190))]), (0, y0 + h - 260))
    colores = _colores_texto(lienzo, (30, 470, 1050, 650), plan, rng)
    for (n, y0, h), frase in zip(paneles, plan["textos"].get("frases", [])[:2]):
        texto, emo = separar_emojis(frase.get("texto") or "")
        emo = frase.get("emoji") or emo
        texto_rico(lienzo, texto, frase.get("resaltado"), emo, 30, 1020, y0 + h - 24,
                   tam_max=74, tam_min=50, max_lineas=2, colores=colores, ancla="abajo",
                   contorno=8, interlinea=1.18)
    return lienzo


def estilo_4a(fotos, plan, rng):
    v = plan["variante"]
    t = plan["textos"]
    lienzo = Image.new("RGBA", (W, H), "#000000")
    slots = plan["slots"]["fotos"]
    con_titulo = bool(v.get("con_titulo", True) and (t.get("titular") or "").strip())
    alto_fotos = 1166 if con_titulo else H
    chicas = slots[1:4]
    if v.get("orientacion", "invertido") == "invertido":
        f = fotos[slots[0]["i"]]
        img, _ = cubrir(f["img"], 560, alto_fotos, _slot_cara(f, slots[0]), objetivo=0.26)
        lienzo.paste(img, (0, 0))
        n = len(chicas)
        alto = (alto_fotos - 6 * (n - 1)) / n
        for k, slot in enumerate(chicas):
            f = fotos[slot["i"]]
            y = int(k * (alto + 6))
            h = int(alto if k < n - 1 else alto_fotos - y)
            img, _ = cubrir(f["img"], 514, h, _slot_cara(f, slot), objetivo=0.28)
            lienzo.paste(img, (566, y))
    else:
        f = fotos[slots[0]["i"]]
        alto_g = int(alto_fotos * 0.55)
        img, _ = cubrir(f["img"], W, alto_g, _slot_cara(f, slots[0]), objetivo=0.30)
        lienzo.paste(img, (0, 0))
        n = len(chicas)
        ancho = (W - 6 * (n - 1)) / n
        for k, slot in enumerate(chicas):
            f = fotos[slot["i"]]
            x = int(k * (ancho + 6))
            w = int(ancho if k < n - 1 else W - x)
            img, _ = cubrir(f["img"], w, alto_fotos - alto_g - 6, _slot_cara(f, slot), objetivo=0.28)
            lienzo.paste(img, (x, alto_g + 6))
    if con_titulo:
        acento1, _ = _acentos(plan)
        d = ImageDraw.Draw(lienzo)
        d.rectangle((0, 1172, W, H), fill="#000000")
        d.rectangle((0, 1172, W, 1180), fill=acento1)
        colores = PARES.get(v.get("par")) or PARES[_elegir_par_titulo(plan, rng)]
        tit, emo = separar_emojis(t.get("titular") or "")
        texto_rico(lienzo, tit, t.get("resaltado"), emo or t.get("emoji", ""), 60, 960, 1265,
                   tam_max=88, tam_min=56, max_lineas=1, colores=colores, ancla="centro", tracking=1)
    return lienzo


def _elegir_par_titulo(plan, rng):
    """Sobre la barra negra se lee cualquier par: se elige por tono."""
    v = plan["variante"]
    opciones = PAR_POR_TONO.get(plan.get("tono")) or list(PARES)
    opciones = [o for o in opciones if o != v.get("evitar_par")] or opciones
    v["par"] = v.get("par_preferido") if v.get("par_preferido") in PARES else rng.choice(opciones)
    return v["par"]


def estilo_5a(fotos, plan, rng):
    v = plan["variante"]
    t = plan["textos"]
    lienzo = Image.new("RGBA", (W, H), "#000000")
    arriba = plan["slots"]["fotos"]
    circulos = plan["slots"]["circulos"]
    if len(arriba) >= 2:
        for k, slot in enumerate(arriba[:2]):
            f = fotos[slot["i"]]
            img, _ = cubrir(f["img"], 537, 672, _slot_cara(f, slot), objetivo=0.22)
            lienzo.paste(img, (k * 543, 0))
    else:
        f = fotos[arriba[0]["i"]]
        img, _ = cubrir(f["img"], W, 672, None)
        lienzo.paste(img, (0, 0))
    # Fondo de abajo: la foto del último círculo, desenfocada y oscurecida.
    fb = fotos[circulos[-1]["i"]]
    fondo, _ = cubrir(fb["img"], W, 672, _slot_cara(fb, circulos[-1]))
    fondo = fondo.resize((int(W * 1.15), int(672 * 1.15))).crop((81, 50, 81 + W, 50 + 672))
    fondo = ImageEnhance.Color(fondo.filter(ImageFilter.GaussianBlur(14))).enhance(1.3)
    lienzo.paste(fondo, (0, 678))
    lienzo.alpha_composite(degradado(W, 672, [(0, (0, 0, 0, 140)), (1, (0, 0, 0, 217))]), (0, 678))
    acento1, acento2 = _acentos(plan)
    rot = -6 if v.get("franja", "diagonal") == "diagonal" else 0
    franja = Image.new("RGBA", (1300, 190), (0, 0, 0, 0))
    ImageDraw.Draw(franja).rectangle((0, 0, 1299, 189), fill="#000000")
    ImageDraw.Draw(franja).rectangle((0, 10, 1299, 179), fill=acento1)
    franja = franja.rotate(-rot if rot else 0, expand=True, resample=Image.BICUBIC)
    y_centro = 1013 if len(circulos) == 2 else 985
    lienzo.alpha_composite(franja, (int(540 - franja.width / 2), int(y_centro - franja.height / 2)))

    anillo_color = acento2 if _lum(_rgb(acento2)) > 0.25 else "#FFFFFF"
    if len(circulos) >= 3:
        diam, anillos, centros, tam_et = 290, [(12, anillo_color), (6, "#000000")], [(190, 985), (540, 985), (890, 985)], 40
    else:
        diam, anillos, centros, tam_et = 400, [(14, anillo_color), (8, "#000000")], [(256, 996), (825, 996)], 52
    etiquetas = t.get("etiquetas") or []
    for k, (slot, (cx, cy)) in enumerate(zip(circulos, centros)):
        f = fotos[slot["i"]]
        foto = recorte_circular(f["img"], _slot_cara(f, slot), diam)
        pegar_circulo(lienzo, foto, cx, cy, anillos, sombra=(0, 18, 50, 180))
        nombre = (etiquetas[k] if k < len(etiquetas) else slot.get("persona") or "").upper()
        if nombre:
            fondo_et = anillo_color if k == 0 else "#FFFFFF"
            _etiqueta(lienzo, nombre[:12], cx, cy + diam / 2 + anillos[0][0] - 10, tam_et, fondo_et)
    if len(circulos) == 2 and v.get("vs", True):
        vs = Image.new("RGBA", (300, 220), (0, 0, 0, 0))
        ImageDraw.Draw(vs).text((150, 110), "VS", font=_fuente(ANTON, 130), fill="#FFFFFF",
                                anchor="mm", stroke_width=10, stroke_fill="#000000")
        vs = vs.rotate(6, resample=Image.BICUBIC)
        lienzo.alpha_composite(vs, (540 - 150, 994 - 110))
    return lienzo


def _etiqueta(lienzo, texto, cx, y, tam, fondo):
    f = _fuente(ANTON, tam)
    ancho = f.getlength(texto) + 2 * max(0, len(texto) - 1) + 56
    alto = tam * 1.45
    caja = Image.new("RGBA", (int(ancho + 40), int(alto + 40)), (0, 0, 0, 0))
    d = ImageDraw.Draw(caja)
    d.rectangle((20, 20, 20 + ancho, 20 + alto), fill=fondo)
    color = "#000000" if _lum(_rgb(fondo)) > 0.3 else "#FFFFFF"
    d.text((20 + ancho / 2, 20 + alto / 2 + 2), texto, font=f, fill=color, anchor="mm")
    caja = caja.rotate(3, expand=True, resample=Image.BICUBIC)
    sombra = Image.new("RGBA", caja.size, (0, 0, 0, 0))
    sombra.putalpha(caja.getchannel("A").point(lambda a: int(a * 0.5)))
    sombra = sombra.filter(ImageFilter.GaussianBlur(10))
    lienzo.alpha_composite(sombra, (int(cx - caja.width / 2), int(y - 20 + 8)))
    lienzo.alpha_composite(caja, (int(cx - caja.width / 2), int(y - 20)))


POS_CIRCULO = {
    "sup_izq": (60, 60), "sup_der": (600, 60),
    "costura_izq": (60, 465), "costura_der": (600, 450), "centro": (330, 465),
    "inf_izq": (60, 870), "inf_der": (600, 870),
}


def _solape(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + ah, by + bh) - max(ay, by)
    return max(0, dx) * max(0, dy)


def estilo_1a(fotos, plan, rng):
    v = plan["variante"]
    lienzo = Image.new("RGBA", (W, H), "#000000")
    slots = plan["slots"]["fotos"][:3]
    n = len(slots)
    alto = 672 if n == 2 else 446
    caras_lienzo = []
    for k, slot in enumerate(slots):
        f = fotos[slot["i"]]
        y = k * (alto + 6)
        h = alto if k < n - 1 else H - y
        cara = _slot_cara(f, slot)
        img, tr = cubrir(f["img"], W, h, cara, objetivo=0.30 if n == 2 else 0.28, alto_cara=0.42)
        lienzo.paste(img, (0, y))
        caras_lienzo += caras_en_marco(f.get("caras"), tr, 0, y, W, h)
    circ = plan["slots"]["circulos"][0]
    diam = 400 if n == 2 else 340
    exterior = diam + 20
    escala = exterior / 420
    # Posición: la pedida, o la que menos tapa caras (anotada en el plan).
    pos = v.get("posicion_circulo")
    if pos not in POS_CIRCULO:
        preferidas = list(POS_CIRCULO)
        rng.shuffle(preferidas)
        mejor = None
        for pid in preferidas:
            x, y = POS_CIRCULO[pid]
            caja = (x, y, exterior, exterior)
            tapa = sum(_solape(caja, c) for c in caras_lienzo)
            if mejor is None or tapa < mejor[0]:
                mejor = (tapa, pid)
        pos = v["posicion_circulo"] = mejor[1]
    x, y = POS_CIRCULO[pos]
    cx, cy = x + 210 * escala, y + 210 * escala
    if y > 800:
        cy = min(cy, H - SEGURO - exterior / 2)
    fc = fotos[circ["i"]]
    foto = recorte_circular(fc["img"], _slot_cara(fc, circ), diam)
    pegar_circulo(lienzo, foto, cx, cy, [(10, "#FFFFFF")], sombra=(0, 12, 40, 150))
    izquierda = cx < W / 2
    enf = v.get("enfasis", "emojis")
    if enf == "emojis":
        emojis = [e for e in (plan["textos"].get("emojis") or []) if e][:2]
        if not emojis:
            emojis = (EMOJIS_POR_TONO.get(plan.get("tono")) or ["👀", "😱"])[:2]
        offs = [(-243, -151, 120, -14), (167, 221, 110, 12)]
        for k, e in enumerate(emojis):
            dx, dy, tam, rot = offs[k]
            if izquierda:
                dx = -dx
            ex = min(max(cx + dx, SEGURO + tam / 2), W - SEGURO - tam / 2)
            ey = min(max(cy + dy, SEGURO + tam / 2), H - SEGURO - tam / 2)
            img = _emoji_img(e, tam)
            if img is not None:
                img = img.rotate(-rot, expand=True, resample=Image.BICUBIC)
                lienzo.alpha_composite(img, (int(ex - img.width / 2), int(ey - img.height / 2)))
    elif enf == "flecha":
        r = exterior / 2 + 20
        lado = 1 if izquierda else -1  # la flecha viene desde el lado libre
        ang = math.radians(-35)
        p1 = (cx + lado * r * math.cos(ang), cy + r * math.sin(ang))
        p0 = (p1[0] + lado * 280, p1[1] - 200)
        p0 = (min(max(p0[0], SEGURO), W - SEGURO), max(p0[1], SEGURO))
        flecha(lienzo, p0, p1, curva=60 * lado)
    return lienzo


ESTILOS = {"1a": estilo_1a, "2a": estilo_2a, "3a": estilo_3a, "4a": estilo_4a, "5a": estilo_5a}


def render(plan, rutas, salida):
    """Dibuja el plan con las fotos de `rutas` y guarda un PNG en `salida`.

    El plan se completa en el lugar con lo que se decidió al dibujar (par de
    colores, posición del círculo) para poder repetirlo idéntico.
    """
    fotos = []
    for k, r in enumerate(rutas):
        info = (plan.get("fotos_info") or [{}] * len(rutas))[k] if k < len(plan.get("fotos_info") or []) else {}
        fotos.append({"img": abrir(r), "caras": info.get("caras") or []})
    rng = random.Random(plan.get("semilla", 1))
    plan.setdefault("variante", {})
    lienzo = ESTILOS[plan["estilo"]](fotos, plan, rng)
    lienzo.convert("RGB").save(salida, "PNG", optimize=True)
    return salida
