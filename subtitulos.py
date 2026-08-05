#!/usr/bin/env python3
"""Subtítulos tipo karaoke para los reels.

Arma un archivo .ass (el formato de subtítulos que entiende ffmpeg vía libass)
con el mismo efecto que se ve en los reels virales: el texto aparece en bloques
cortos, todo en mayúsculas, y la palabra que se está pronunciando en ese
instante se prende de otro color mientras las demás quedan en blanco.

Cómo se logra el efecto: en vez de usar el karaoke nativo de ASS —que deja
pintadas todas las palabras ya dichas y no es lo que se ve en esos videos— se
escribe UN renglón por palabra. Cada renglón repite el bloque completo y solo
cambia de color la palabra activa. Son muchos renglones, pero el archivo pesa
nada y libass los dibuja sin despeinarse.

Las marcas de tiempo pueden venir de dos lados:

- Si el servicio de voz devuelve el momento exacto de cada palabra, se usan tal
  cual y el calce es perfecto.
- Si no las devuelve, se reparte la duración total del audio entre las palabras
  según cuánto tarda decir cada una (letras, más una pausa extra en las comas y
  los puntos). Queda bien; solo comparando lado a lado se nota la diferencia.

Uso desde otro módulo:

    import subtitulos
    subtitulos.escribir_ass(texto, duracion, ruta_salida, marcas=None)
"""
import re
from pathlib import Path

# Lienzo de referencia. Tiene que coincidir con el del video para que los
# tamaños y márgenes signifiquen lo mismo.
ANCHO = 1080
ALTO = 1920

# Cuántas palabras entran como máximo en un bloque en pantalla.
MAX_PALABRAS = 4
# Y cuántos caracteres, para que un bloque de palabras largas no se desborde.
MAX_CARACTERES = 30

# Colores en el formato de ASS, que va al revés de lo habitual: &HAABBGGRR.
BLANCO = "&H00FFFFFF"
AMARILLO = "&H0000D7FF"
VERDE = "&H0000FF66"
NEGRO = "&H00000000"

# La palabra activa alterna entre estos dos, igual que en el video de muestra.
RESALTES = (AMARILLO, VERDE)

# Altura del texto sobre el borde de abajo del lienzo.
MARGEN_ABAJO = 620

# Peso extra que se le da a los signos de puntuación al repartir el tiempo:
# después de un punto la voz respira, después de una coma un poquito.
PAUSAS = {".": 3.5, "!": 3.5, "?": 3.5, ":": 2.0, ";": 2.0, ",": 1.8, "…": 3.5}


def _escapar(texto):
    """Deja el texto listo para meterlo en un renglón de ASS."""
    return (
        texto.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
    )


def _hhmmss(segundos):
    """Formato de tiempo de ASS: h:mm:ss.cc (centésimas, no milésimas)."""
    if segundos < 0:
        segundos = 0
    horas = int(segundos // 3600)
    minutos = int((segundos % 3600) // 60)
    resto = segundos - horas * 3600 - minutos * 60
    return f"{horas}:{minutos:02d}:{resto:05.2f}"


def partir_palabras(texto):
    """Separa el texto en palabras, conservando la puntuación pegada."""
    limpio = re.sub(r"\s+", " ", (texto or "").strip())
    if not limpio:
        return []
    return limpio.split(" ")


def _peso(palabra):
    """Cuánto tiempo se lleva una palabra, en unidades arbitrarias.

    Las letras mandan, pero un signo de puntuación al final agrega respiro.
    El mínimo evita que un "y" suelto pase volando.
    """
    letras = max(1, len(re.sub(r"[^\wáéíóúüñÁÉÍÓÚÜÑ]", "", palabra)))
    extra = 0.0
    for signo, valor in PAUSAS.items():
        if palabra.endswith(signo):
            extra = max(extra, valor)
    return letras + 1.2 + extra


def repartir_tiempos(palabras, duracion, desde=0.0):
    """Devuelve [(palabra, inicio, fin)] repartiendo la duración por peso."""
    if not palabras:
        return []
    pesos = [_peso(p) for p in palabras]
    total = sum(pesos) or 1.0
    marcas = []
    t = desde
    disponible = max(0.1, duracion - desde)
    for palabra, peso in zip(palabras, pesos):
        dur = disponible * peso / total
        marcas.append((palabra, t, t + dur))
        t += dur
    return marcas


def agrupar_bloques(marcas, max_palabras=MAX_PALABRAS, max_caracteres=MAX_CARACTERES):
    """Junta las palabras en los bloques que se ven en pantalla.

    Corta por cantidad, por largo, y —lo más importante— después de un punto,
    para que dos oraciones distintas nunca compartan el mismo bloque.
    """
    bloques, actual, largo = [], [], 0
    for marca in marcas:
        palabra = marca[0]
        cabe = len(actual) < max_palabras and (largo + len(palabra) + 1) <= max_caracteres
        if actual and not cabe:
            bloques.append(actual)
            actual, largo = [], 0
        actual.append(marca)
        largo += len(palabra) + 1
        if palabra.endswith((".", "!", "?", "…")):
            bloques.append(actual)
            actual, largo = [], 0
    if actual:
        bloques.append(actual)
    return bloques


def _cabecera(tamano=76, fuente="Anton", margen_abajo=MARGEN_ABAJO):
    """Encabezado del archivo .ass: lienzo y estilo del texto."""
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {ANCHO}
PlayResY: {ALTO}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Voz,{fuente},{tamano},{BLANCO},{BLANCO},{NEGRO},{NEGRO},0,0,0,0,100,100,0,0,1,5,2,2,60,60,{margen_abajo},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _renglon(inicio, fin, texto):
    return f"Dialogue: 0,{_hhmmss(inicio)},{_hhmmss(fin)},Voz,,0,0,0,,{texto}\n"


def construir_eventos(bloques):
    """Arma los renglones del .ass: uno por palabra activa."""
    renglones = []
    for n, bloque in enumerate(bloques):
        color = RESALTES[n % len(RESALTES)]
        for i, (_, inicio, fin) in enumerate(bloque):
            partes = []
            for j, (palabra, _, _) in enumerate(bloque):
                limpia = _escapar(palabra.upper())
                if i == j:
                    partes.append(f"{{\\c{color}}}{limpia}{{\\c{BLANCO}}}")
                else:
                    partes.append(limpia)
            # El primer renglón del bloque entra con un golpecito de escala,
            # que es lo que le da el "rebote" a este tipo de subtítulo.
            entrada = "{\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}" if i == 0 else ""
            renglones.append(_renglon(inicio, fin, entrada + " ".join(partes)))
    return renglones


def escribir_ass(texto, duracion, salida, marcas=None, tamano=76, fuente="Anton",
                 margen_abajo=MARGEN_ABAJO):
    """Escribe el archivo .ass y devuelve su ruta.

    texto        -- el guion completo tal como se narró
    duracion     -- cuánto dura el audio, en segundos
    salida       -- ruta del archivo .ass a crear
    marcas       -- opcional, [(palabra, inicio, fin)] si el servicio de voz las
                    devolvió; si viene vacío se reparte el tiempo por peso
    margen_abajo -- a qué altura del borde inferior se apoya el texto; sirve
                    para que el subtítulo caiga siempre dentro de la franja
                    central aunque cambien los altos de las franjas
    """
    salida = Path(salida)
    if marcas:
        tiempos = [(p, float(a), float(b)) for p, a, b in marcas if str(p).strip()]
    else:
        tiempos = repartir_tiempos(partir_palabras(texto), float(duracion))
    if not tiempos:
        salida.write_text(_cabecera(tamano, fuente, margen_abajo), encoding="utf-8")
        return salida
    bloques = agrupar_bloques(tiempos)
    contenido = _cabecera(tamano, fuente, margen_abajo) + "".join(
        construir_eventos(bloques)
    )
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(contenido, encoding="utf-8")
    return salida


if __name__ == "__main__":
    import sys

    demo = (
        "Lo que empezó como una simple dinámica para pasar el tiempo terminó "
        "convirtiéndose en uno de los temas del reality. Las opiniones quedaron "
        "completamente divididas."
    )
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/demo.ass")
    escribir_ass(demo, 30.0, destino)
    print(f"OK -> {destino}")
