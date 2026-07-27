#!/usr/bin/env python3
"""
Compositor de imagenes para posts.
Une 2-3 screenshots en vertical y sobrepone título + frases resaltantes
con el estilo: fuente display bold, relleno de color y contorno grueso.

Uso:
  python3 compose_post.py spec.json salida.jpg

spec.json:
{
  "width": 1080,
  "title": {"text": "TODO ES UN JUEGO", "image": 0},   # opcional
  "images": [
    {"path": "img1.jpg", "lines": [
        {"text": "Josh: Perdon por lo de Stefano!", "color": "white"}
    ]},
    {"path": "img2.jpg", "lines": [
        {"text": "Fabio: Es un juego!", "color": "orange"},
        {"text": "Yo no hago las cosas personales", "color": "white"}
    ]}
  ],
  "watermark": "TEXTO"                     # opcional
}
"""
import json, sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_TITLE = os.path.join(BASE_DIR, "fonts", "LuckiestGuy-Regular.ttf")
FONT_LINE  = os.path.join(BASE_DIR, "fonts", "LuckiestGuy-Regular.ttf")
FONT_WM    = os.path.join(BASE_DIR, "fonts", "Anton-Regular.ttf")

COLORS = {
    "orange": (255, 166, 33),
    "yellow": (255, 205, 60),
    "white":  (255, 255, 255),
    "red":    (255, 70, 60),
}

def load_resized(path, width):
    im = Image.open(path).convert("RGB")
    r = width / im.width
    return im.resize((width, round(im.height * r)), Image.LANCZOS)

def parse_marks(text):
    """Separa marcadores ~palabra~ -> (texto limpio, spans de censura)."""
    spans, clean, in_mark, start = [], "", False, 0
    for ch in text:
        if ch == "~":
            if not in_mark:
                in_mark, start = True, len(clean)
            else:
                in_mark = False
                spans.append((start, len(clean)))
        else:
            clean += ch
    return clean, spans

def measure(draw, text, font):
    return draw.textlength(parse_marks(text)[0], font=font)

def wrap_text(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if measure(draw, t, font) <= max_w:
            cur = t
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines

def fit_font(draw, text, path, max_w, start, minimum=34):
    size = start
    while size > minimum:
        f = ImageFont.truetype(path, size)
        if all(measure(draw, l, f) <= max_w for l in wrap_text(draw, text, f, max_w)):
            # ensure no more than 2 wrapped lines at this size
            if len(wrap_text(draw, text, f, max_w)) <= 2:
                return f
        size -= 4
    return ImageFont.truetype(path, minimum)

def draw_outlined(canvas, xy_center, text, font, fill, stroke=(20, 20, 20), stroke_w=None):
    """Texto centrado con contorno grueso + sombra suave.
    Los segmentos marcados con ~tildes~ reciben una línea de censura encima."""
    clean, spans = parse_marks(text)
    d = ImageDraw.Draw(canvas)
    x, y = xy_center
    sw = stroke_w or max(4, font.size // 9)
    # sombra
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ds = ImageDraw.Draw(shadow)
    ds.text((x + 3, y + 5), clean, font=font, fill=(0, 0, 0, 180),
            anchor="mm", stroke_width=sw, stroke_fill=(0, 0, 0, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(4))
    canvas.alpha_composite(shadow)
    d.text((x, y), clean, font=font, fill=fill, anchor="mm",
           stroke_width=sw, stroke_fill=stroke)
    # líneas de censura sobre los segmentos marcados
    if spans:
        total_w = d.textlength(clean, font=font)
        left = x - total_w / 2
        for s, e in spans:
            x0 = left + d.textlength(clean[:s], font=font) - font.size * 0.06
            x1 = left + d.textlength(clean[:e], font=font) + font.size * 0.06
            tilt = font.size * 0.10
            thick = max(5, int(font.size * 0.14))
            d.line([(x0, y + tilt), (x1, y - tilt)], fill=(25, 25, 25, 235), width=thick)

def main(spec_path, out_path):
    spec = json.load(open(spec_path))
    W = spec.get("width", 1080)
    imgs = [load_resized(i["path"], W) for i in spec["images"]]
    H = sum(i.height for i in imgs)
    canvas = Image.new("RGBA", (W, H))
    offsets = []
    y = 0
    for im in imgs:
        canvas.paste(im, (0, y))
        offsets.append(y)
        y += im.height

    d = ImageDraw.Draw(canvas)
    margin = int(W * 0.05)
    max_w = W - 2 * margin

    # Título
    t = spec.get("title")
    if t:
        idx = t.get("image", 0)
        f = fit_font(d, t["text"], FONT_TITLE, max_w, 96, 48)
        lines = wrap_text(d, t["text"], f, max_w)
        line_h = f.size * 1.15
        y0 = offsets[idx] + int(imgs[idx].height * t.get("pos", 0.16))
        for i, l in enumerate(lines):
            draw_outlined(canvas, (W // 2, y0 + i * line_h), l, f,
                          COLORS.get(t.get("color", "orange")))

    # Frases por imagen
    for idx, im_spec in enumerate(spec["images"]):
        lines_spec = im_spec.get("lines", [])
        if not lines_spec:
            continue
        blocks = []  # (texto envuelto, fuente, color)
        for ls in lines_spec:
            f = fit_font(d, ls["text"], FONT_LINE, max_w, spec.get("line_size", 64), 30)
            wrapped = wrap_text(d, ls["text"], f, max_w)
            blocks.append((wrapped, f, COLORS.get(ls.get("color", "white"))))
        total_h = sum(len(w) * f.size * 1.2 + 10 for w, f, _ in blocks)
        pos = im_spec.get("pos", 0.80)  # fracción vertical dentro de la foto
        y0 = offsets[idx] + int(imgs[idx].height * pos) - total_h / 2
        for wrapped, f, color in blocks:
            for l in wrapped:
                draw_outlined(canvas, (W // 2, y0 + f.size * 0.6), l, f, color)
                y0 += f.size * 1.2
            y0 += 10

    # Marca de agua
    wm = spec.get("watermark")
    if wm:
        f = ImageFont.truetype(FONT_WM, 30)
        d.text((W - margin, offsets[-1] + 40), wm, font=f, anchor="rm",
               fill=(255, 255, 255, 200), stroke_width=2, stroke_fill=(0, 0, 0, 160))

    canvas.convert("RGB").save(out_path, quality=92)
    print(f"OK -> {out_path} ({W}x{H})")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
