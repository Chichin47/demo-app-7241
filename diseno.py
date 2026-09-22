#!/usr/bin/env python3
"""Arma el diseño de un post a partir de una descripción y 2 a 5 fotos.

Pasos:
  1. Reconoce las caras de cada foto (rostros.py): quién sale y dónde.
  2. Le pide a Claude la descripción del post (el mismo pedido de siempre) y,
     aparte, el estilo y los textos de la imagen (titular, frases, emojis...).
  3. Decide qué foto va en cada lugar según las caras, y la variante (paleta,
     fondo, orientación, posición del círculo...).
  4. Dibuja (estilos.py).

El resultado es un PLAN que se puede volver a dibujar idéntico (para mandar
el archivo en alta) y la ruta del PNG. Nada de esto publica en ninguna
página: por ahora todo vuelve al chat de Telegram para revisarlo.
"""
import json
import random
import time

import estilos

ESTILOS_VALIDOS = ["1a", "2a", "3a", "4a", "5a"]
TONOS = ["pelea", "sorpresa", "tristeza", "triunfo", "intriga", "verguenza", "chisme"]

SISTEMA = """Sos el editor gráfico de Universo Reality TV, una página de Facebook e \
Instagram sobre reality shows de México (La Casa de los Famosos, La Granja VIP y \
parecidos). Te paso la nota de un post, las frases que ya se escribieron para la \
imagen y quién aparece en cada foto. Elegís UNO de estos 5 estilos de imagen y \
escribís sus textos:

1a Detalle + Reacciones: 2 o 3 fotos apiladas y un círculo con la cara de UNA \
persona, con 2 emojis o una flecha señalándola. Sin texto sobre la imagen. Sirve \
para "miren la reacción de X", un gesto, un detalle.
2a Duelo vertical: 2 fotos lado a lado (una persona en cada mitad) y UN titular \
corto y fuerte abajo, con una palabra resaltada, más un subtítulo de una línea. \
Sirve para enfrentamientos, anuncios fuertes, revelaciones.
3a Clásico renovado: 2 fotos apiladas, cada una con una frase "NOMBRE: LO QUE \
DIJO" y un emoji al final. Sirve cuando hay diálogo entre dos personas.
4a Mosaico: 1 foto grande del protagonista y 2 o 3 chicas, con un título de una \
línea abajo. Sirve para notas con varios momentos o varias personas.
5a Bloque con círculos: la escena arriba y abajo 2 o 3 caras en círculos con su \
nombre en una etiqueta (con "VS" si son 2). Sirve para rivalidades, nominados, \
quiénes están involucrados.

Reglas de los textos de la imagen:
- Van en MAYÚSCULAS, sin hashtags, sin comillas.
- titular (2a y 4a): corto y contundente, máximo 22 letras. En 2a puede ocupar 2 \
renglones (hasta 24 letras); en 4a debe entrar en 1 renglón.
- resaltado: la palabra o dos palabras MÁS importantes del titular o de la frase, \
copiadas tal cual como aparecen ahí. Es lo que va en otro color.
- subtitulo (2a): una línea, máximo 34 letras, puede terminar con un emoji.
- frases (3a): exactamente 2, formato "NOMBRE: FRASE", máximo 42 letras cada una, \
con su "persona" (el nombre como aparece en las fotos), su "resaltado" y un \
"emoji" (1 o 2 emojis) que vaya con el tono de esa frase.
- emojis (1a): 2 emojis que vayan con el tono.
- etiquetas (5a): los nombres de las personas de los círculos, en el mismo orden \
que "personas".
- Respetá la censura de maquillaje de las frases propuestas (vocales cambiadas \
por números en groserías). No inventes datos que no estén en la nota.
- tono: uno de pelea, sorpresa, tristeza, triunfo, intriga, verguenza, chisme.
- personas: las personas protagonistas de la nota que APARECEN en las fotos, de \
la más importante a la menos. Usá los nombres tal cual te los paso.
- circulo (1a): a quién va el círculo. enfasis (1a): "emojis", "flecha" o "ninguno".
- caption: SOLO si la instrucción del administrador pide cambiar la descripción \
del post, escribí la descripción nueva completa; si no, null.

Respondé SOLO un objeto JSON con estas claves: estilo, tono, personas, titular, \
resaltado, subtitulo, frases, emojis, circulo, enfasis, etiquetas, caption. \
Completá SIEMPRE titular y frases aunque el estilo elegido no los use (así se \
puede cambiar de estilo sin volver a preguntarte). Ejemplo de forma:
{"estilo": "3a", "tono": "pelea", "personas": ["Pascal", "Kevyn"], \
"titular": "¡PAGA TU DEUDA!", "resaltado": "DEUDA", "subtitulo": "PASCAL EXPLOTA CONTRA KEVYN 💥", \
"frases": [{"persona": "Pascal", "texto": "PASCAL: ESTÁS PAGANDO TU DEUDA", "resaltado": "DEUDA", "emoji": "💸😡"}, \
{"persona": "Kevyn", "texto": "KEVYN: FUERON 2 DÍAS, ESTABA ENFERMO", "resaltado": "ENFERMO", "emoji": "🤒"}], \
"emojis": ["😡", "💥"], "circulo": "Pascal", "enfasis": "emojis", "etiquetas": ["PASCAL", "KEVYN"], "caption": null}"""


def log(msg):
    print(f"[diseno] {msg}", flush=True)


# --------------------------------------------------------------------------
# 1. Caras
# --------------------------------------------------------------------------

def leer_caras(rutas):
    """Por cada foto: sus caras (de la más grande a la más chica) con nombre."""
    info = []
    try:
        import rostros
        db = rostros.cargar()
    except Exception as e:  # noqa: BLE001
        log(f"Sin reconocimiento de caras ({e}).")
        return [{"caras": []} for _ in rutas]
    for r in rutas:
        try:
            img = rostros.leer_imagen(r)
            caras = rostros.reconocer(img, db) if db.get("personas") else [
                {"caja": tuple(int(v) for v in f[:4]), "nombre": None, "puntaje": 0}
                for f in rostros.detectar(img)]
            info.append({
                "ancho": int(img.shape[1]), "alto": int(img.shape[0]),
                "caras": [{"caja": [int(v) for v in c["caja"]], "nombre": c.get("nombre"),
                           "puntaje": round(float(c.get("puntaje") or 0), 3)} for c in caras],
            })
        except Exception as e:  # noqa: BLE001
            log(f"No se pudieron leer las caras de una foto ({e}).")
            info.append({"caras": []})
    return info


def resumen_fotos(info):
    lineas = []
    for i, f in enumerate(info, 1):
        caras = f.get("caras") or []
        if not caras:
            lineas.append(f"Foto {i}: no se ve ninguna cara clara.")
            continue
        partes = []
        alto = f.get("alto") or 1
        for c in caras[:5]:
            tam = "grande" if c["caja"][3] / alto > 0.25 else ("mediana" if c["caja"][3] / alto > 0.1 else "chica")
            partes.append(f"{c['nombre'] or 'alguien sin identificar'} (cara {tam})")
        lineas.append(f"Foto {i}: " + ", ".join(partes) + ".")
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# 2. Textos (Claude)
# --------------------------------------------------------------------------

def _claude_por_defecto(system, pedido):
    import poll_and_publish as bot
    return bot._correr_claude_code(system, pedido)


def _extraer_json(texto):
    """El primer objeto JSON completo que aparezca en el texto."""
    t = texto or ""
    inicio = t.find("{")
    while inicio != -1:
        hondura, en_cadena, escape = 0, False, False
        for i in range(inicio, len(t)):
            c = t[i]
            if en_cadena:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    en_cadena = False
                continue
            if c == '"':
                en_cadena = True
            elif c == "{":
                hondura += 1
            elif c == "}":
                hondura -= 1
                if hondura == 0:
                    try:
                        dato = json.loads(t[inicio:i + 1])
                        return dato if isinstance(dato, dict) else None
                    except Exception:  # noqa: BLE001
                        break
        inicio = t.find("{", inicio + 1)
    return None


def estilos_posibles(info):
    n = len(info)
    con_cara = sum(1 for f in info if f.get("caras"))
    total_caras = sum(len(f.get("caras") or []) for f in info)
    posibles = []
    if n >= 2:
        posibles += ["1a", "2a", "3a"]
    if n >= 3:
        posibles.append("4a")
    if n >= 2 and total_caras >= 2 and con_cara >= 1:
        posibles.append("5a")
    return sorted(posibles) or ["3a"]


def pedir_textos(descripcion, edit, info, estilo_pedido=None, instrucciones=None,
                 anteriores=None, pedir_claude=None):
    pedir_claude = pedir_claude or _claude_por_defecto
    posibles = estilos_posibles(info)
    frases = [l.get("text") for l in (edit or {}).get("lines") or [] if l.get("text")]
    pedido = (
        f"Nota original del post:\n---\n{descripcion}\n---\n\n"
        f"Descripción que se va a publicar:\n---\n{(edit or {}).get('caption') or ''}\n---\n\n"
        f"Frases propuestas para la imagen (ya censuradas): {json.dumps(frases, ensure_ascii=False)}\n\n"
        f"Fotos ({len(info)}):\n{resumen_fotos(info)}\n\n"
        f"Estilos posibles con estas fotos: {', '.join(posibles)}."
    )
    if estilo_pedido:
        pedido += f"\n\nEl administrador pidió el estilo {estilo_pedido}: usá ese."
    if instrucciones:
        pedido += ("\n\nInstrucciones del administrador para esta versión (tienen "
                   "prioridad):\n- " + "\n- ".join(instrucciones))
    if anteriores:
        pedido += ("\n\nVersiones anteriores que NO convencieron (hacé algo distinto: "
                   "otro titular, otras palabras, otro enfoque):\n"
                   + json.dumps(anteriores[-3:], ensure_ascii=False))
    ultimo_error = None
    for intento in range(2):
        texto = pedir_claude(SISTEMA, pedido if not ultimo_error else
                             pedido + f"\n\nTu respuesta anterior no sirvió ({ultimo_error}). "
                                      "Respondé SOLO el objeto JSON.")
        datos = _extraer_json(texto) if texto else None
        if isinstance(datos, dict) and datos.get("estilo"):
            return datos
        ultimo_error = "no vino un objeto JSON con 'estilo'"
    raise RuntimeError("Claude no devolvió el diseño")


# --------------------------------------------------------------------------
# 3. Plan
# --------------------------------------------------------------------------

def _clave(n):
    return (n or "").strip().lower()


def _mejor_foto_de(info, nombre, excluir=()):
    mejor = None
    for i, f in enumerate(info):
        if i in excluir:
            continue
        for c in f.get("caras") or []:
            if nombre and _clave(c.get("nombre")) == _clave(nombre):
                rel = c["caja"][3] / max(1, f.get("alto") or 1)
                if mejor is None or rel > mejor[0]:
                    mejor = (rel, i)
    return mejor[1] if mejor else None


def _por_cara_mayor(info, excluir=()):
    orden = []
    for i, f in enumerate(info):
        if i in excluir:
            continue
        caras = f.get("caras") or []
        rel = caras[0]["caja"][3] / max(1, f.get("alto") or 1) if caras else 0
        orden.append((rel, i))
    return [i for _, i in sorted(orden, reverse=True)]


def _escena(info, excluir=()):
    """La foto con más gente (y más ancha), para usar de escena."""
    orden = []
    for i, f in enumerate(info):
        if i in excluir:
            continue
        ancho = (f.get("ancho") or 1) / max(1, f.get("alto") or 1)
        orden.append((len(f.get("caras") or []), ancho, -i, i))
    orden.sort(reverse=True)
    return orden[0][3] if orden else None


def _nombres_presentes(info):
    vistos = []
    for f in info:
        for c in f.get("caras") or []:
            if c.get("nombre") and c["nombre"] not in vistos:
                vistos.append(c["nombre"])
    return vistos


def _texto_de(x):
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        for k in ("texto", "text", "frase", "linea", "line"):
            if isinstance(x.get(k), str) and x[k].strip():
                return x[k].strip()
    return ""


def normalizar(datos, lineas=None):
    """Deja la respuesta de Claude en la forma que espera el plan, venga como
    venga (frases como texto suelto, claves en inglés, campos vacíos), y
    completa lo que falte con las frases de la edición de siempre."""
    datos = dict(datos or {})
    tono = datos.get("tono") if datos.get("tono") in TONOS else "chisme"
    frases = []
    for f in datos.get("frases") or datos.get("phrases") or []:
        texto = _texto_de(f)
        if not texto:
            continue
        d = dict(f) if isinstance(f, dict) else {}
        persona = d.get("persona") or d.get("nombre")
        if not persona and ":" in texto[:25]:
            persona = texto.split(":", 1)[0].strip().title()
        texto, emo = estilos.separar_emojis(texto)
        frases.append({"texto": texto, "persona": persona,
                       "resaltado": d.get("resaltado") or d.get("highlight") or "",
                       "emoji": d.get("emoji") or emo})
    # Si Claude no dejó frases, se usan las de la edición de siempre.
    for l in lineas or []:
        if len(frases) >= 2:
            break
        texto = _texto_de(l)
        if texto and all(texto != f["texto"] for f in frases):
            texto, emo = estilos.separar_emojis(texto)
            frases.append({"texto": texto, "persona": None, "resaltado": "", "emoji": emo})
    emojis_tono = estilos.EMOJIS_POR_TONO.get(tono) or ["👀"]
    for k, f in enumerate(frases):
        if not f["emoji"]:
            f["emoji"] = emojis_tono[k % len(emojis_tono)]
        if not f["resaltado"]:
            # la palabra más larga después del "NOMBRE:" es la que se destaca
            cuerpo = f["texto"].split(":", 1)[-1]
            palabras = [w.strip(".,¡!¿?") for w in cuerpo.split()]
            f["resaltado"] = max(palabras, key=len) if palabras else ""
    datos["frases"] = frases
    titular = _texto_de(datos.get("titular")) or (frases[0]["texto"].split(":", 1)[-1].strip() if frases else "")
    datos["titular"] = titular
    if not datos.get("resaltado") and titular:
        palabras = [w.strip(".,¡!¿?") for w in titular.split()]
        datos["resaltado"] = max(palabras, key=len) if palabras else ""
    datos["tono"] = tono
    if not isinstance(datos.get("emojis"), list):
        datos["emojis"] = [e for e in estilos.RE_EMOJI.findall(str(datos.get("emojis") or ""))]
    return datos


def armar_plan(datos, info, semilla, anterior=None, estilo_pedido=None, lineas=None):
    datos = normalizar(datos, lineas)
    rng = random.Random(semilla)
    posibles = estilos_posibles(info)
    estilo = estilo_pedido or datos.get("estilo")
    if estilo not in posibles:
        estilo = next((e for e in ["3a", "2a", "1a", "4a", "5a"] if e in posibles), posibles[0])
    tono = datos.get("tono") if datos.get("tono") in TONOS else "chisme"
    presentes = _nombres_presentes(info)
    personas = [p for p in (datos.get("personas") or []) if any(_clave(p) == _clave(q) for q in presentes)]
    personas = [next(q for q in presentes if _clave(q) == _clave(p)) for p in personas]
    for q in presentes:
        if q not in personas:
            personas.append(q)
    n = len(info)
    ant_var = (anterior or {}).get("variante", {})

    # Paleta según el tono, sin repetir la de la versión anterior si se puede.
    paletas = list(estilos.PALETA_POR_TONO.get(tono) or estilos.PALETAS)
    if ant_var.get("paleta") in paletas and len(paletas) > 1:
        paletas.remove(ant_var["paleta"])
    variante = {"paleta": rng.choice(paletas), "evitar_par": ant_var.get("par")}
    textos = {}
    slots = {}

    if estilo == "2a":
        a = _mejor_foto_de(info, personas[0]) if personas else None
        b = _mejor_foto_de(info, personas[1], excluir={a}) if len(personas) > 1 else None
        orden = _por_cara_mayor(info, excluir={x for x in (a, b) if x is not None})
        a = a if a is not None else orden.pop(0)
        b = b if b is not None else orden.pop(0)
        slots["fotos"] = [{"i": a, "persona": personas[0] if personas else None},
                          {"i": b, "persona": personas[1] if len(personas) > 1 else None}]
        if rng.random() < 0.4:
            slots["fotos"].reverse()
        variante["fondo"] = rng.choice(["blanco", "blanco", "negro"])
        textos = {"titular": datos.get("titular") or "", "resaltado": datos.get("resaltado") or "",
                  "subtitulo": datos.get("subtitulo") or ""}

    elif estilo == "3a":
        frases = [f for f in (datos.get("frases") or []) if isinstance(f, dict) and f.get("texto")][:2]
        usadas = set()
        fotos = []
        for fr in frases:
            nombre = fr.get("persona")
            i = _mejor_foto_de(info, nombre, excluir=usadas) if nombre else None
            if i is None:
                resto = _por_cara_mayor(info, excluir=usadas)
                i = resto[0] if resto else 0
            usadas.add(i)
            fotos.append({"i": i, "persona": nombre})
        while len(fotos) < 2:
            resto = _por_cara_mayor(info, excluir=usadas) or [0]
            usadas.add(resto[0])
            fotos.append({"i": resto[0]})
        slots["fotos"] = fotos
        variante["fondo"] = rng.choice(["blanco", "blanco", "negro"])
        textos = {"frases": frases or [{"texto": datos.get("titular") or "", "resaltado": datos.get("resaltado")}]}

    elif estilo == "4a":
        principal = _mejor_foto_de(info, personas[0]) if personas else None
        if principal is None:
            principal = _por_cara_mayor(info)[0]
        resto = [i for i in _por_cara_mayor(info, excluir={principal})][:3 if n >= 4 else 2]
        slots["fotos"] = [{"i": principal, "persona": personas[0] if personas else None}] + [{"i": i} for i in resto]
        f = info[principal]
        apaisada = (f.get("ancho") or 1) > (f.get("alto") or 1) * 1.2
        variante["orientacion"] = rng.choices(["normal", "invertido"], weights=[3, 2] if apaisada else [1, 3])[0]
        variante["con_titulo"] = bool(datos.get("titular"))
        textos = {"titular": datos.get("titular") or "", "resaltado": datos.get("resaltado") or "",
                  "emoji": (datos.get("emojis") or [""])[0] if not estilos.RE_EMOJI.search(datos.get("titular") or "") else ""}

    elif estilo == "5a":
        circulos = []
        usadas = set()
        for p in personas[:3]:
            i = _mejor_foto_de(info, p, excluir=usadas)
            if i is None:
                i = _mejor_foto_de(info, p)
            if i is not None:
                circulos.append({"i": i, "persona": p})
                usadas.add(i)
        # Si faltan caras con nombre, se completan con las caras más grandes.
        if len(circulos) < 2:
            for i in _por_cara_mayor(info, excluir=usadas):
                if info[i].get("caras"):
                    circulos.append({"i": i, "cara": info[i]["caras"][0]["caja"]})
                    usadas.add(i)
                if len(circulos) >= 2:
                    break
        while len(circulos) < 2:  # misma foto, otra cara
            f0 = next((i for i, f in enumerate(info) if len(f.get("caras") or []) >= 2), 0)
            caras = info[f0].get("caras") or [{"caja": None}]
            circulos.append({"i": f0, "cara": caras[min(len(circulos), len(caras) - 1)]["caja"]})
        escena = _escena(info, excluir=usadas)
        if escena is None:
            escena = _escena(info)
        arriba = [{"i": escena}]
        libres = [i for i in range(n) if i not in usadas and i != escena]
        if n >= 4 and libres and len(circulos) == 2:
            arriba.append({"i": libres[0]})
        slots["fotos"] = arriba
        slots["circulos"] = circulos[:3]
        variante["franja"] = rng.choice(["diagonal", "diagonal", "recta"])
        etiquetas = [e for e in (datos.get("etiquetas") or []) if e]
        textos = {"etiquetas": [c.get("persona") or (etiquetas[k] if k < len(etiquetas) else "")
                                for k, c in enumerate(circulos[:3])]}

    else:  # 1a
        quien = datos.get("circulo") if datos.get("circulo") in personas else (personas[0] if personas else None)
        circ_i = _mejor_foto_de(info, quien) if quien else None
        if circ_i is None:
            circ_i = _por_cara_mayor(info)[0]
        if n == 2:
            rects = [_escena(info), None]
            rects[1] = 1 - rects[0]
        else:
            rects = [i for i in range(n) if i != circ_i]
            escena = _escena(info, excluir={circ_i})
            rects.remove(escena)
            rects = [escena] + rects
            rects = rects[:3]
            if len(rects) == 3 and rng.random() < 0.5:
                rects = rects[:2]
        slots["fotos"] = [{"i": i} for i in rects]
        slots["circulos"] = [{"i": circ_i, "persona": quien}]
        enf = datos.get("enfasis") if datos.get("enfasis") in ("emojis", "flecha", "ninguno") else None
        variante["enfasis"] = enf or rng.choice(["emojis", "emojis", "flecha"])
        textos = {"emojis": [e for e in (datos.get("emojis") or []) if e][:2]}

    return {
        "estilo": estilo, "tono": tono, "semilla": semilla, "slots": slots,
        "textos": textos, "variante": variante, "fotos_info": info,
        "creado": time.time(),
    }


# --------------------------------------------------------------------------
# 4. Todo junto
# --------------------------------------------------------------------------

def componer(datos, info, rutas, salida, *, edit=None, estilo_pedido=None,
             anterior_plan=None, semilla=None):
    """Con la respuesta de Claude ya en mano: arma el plan y dibuja."""
    semilla = semilla if semilla is not None else random.randrange(1, 10 ** 9)
    plan = armar_plan(datos, info, semilla, anterior_plan, estilo_pedido,
                      lineas=(edit or {}).get("lines"))
    t = plan["textos"]
    log(f"Textos de la imagen: titular={t.get('titular')!r}, "
        f"frases={[f.get('texto') for f in t.get('frases') or []]}, "
        f"etiquetas={t.get('etiquetas')}, emojis={t.get('emojis')}.")
    estilos.render(plan, rutas, salida)
    log(f"Diseño {plan['estilo']} ({plan['tono']}, {plan['variante'].get('paleta')}, "
        f"{plan['variante'].get('par')}).")
    return plan


def disenar(descripcion, rutas, salida, *, edit, info=None, estilo_pedido=None,
            instrucciones=None, anteriores=None, anterior_plan=None, semilla=None,
            pedir_claude=None):
    """Devuelve (plan, datos_de_claude). Deja el PNG en `salida`."""
    info = info if info is not None else leer_caras(rutas)
    datos = pedir_textos(descripcion, edit, info, estilo_pedido, instrucciones,
                         anteriores, pedir_claude)
    plan = componer(datos, info, rutas, salida, edit=edit, estilo_pedido=estilo_pedido,
                    anterior_plan=anterior_plan, semilla=semilla)
    return plan, datos
