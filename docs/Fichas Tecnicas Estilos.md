# Universo Reality TV — Fichas técnicas de los 5 estilos de post

Fuente: `Post Pascal vs Kevyn.dc.html`. Todas las medidas se tomaron del render real.
Unidades: px sobre el lienzo de 1080×1350. En Canva, el tamaño de fuente se ingresa con el mismo número (en un diseño de 1080 px de ancho, 1 unidad de fuente = 1 px).
Coordenadas: `x, y` = esquina superior izquierda de la capa; el origen (0,0) es la esquina superior izquierda del lienzo.

---

## 0. Reglas comunes

**Lienzo**
- 1080 × 1350 px, vertical 4:5, fondo `#000000` (en 2a y 3a el fondo es `#FFFFFF`, porque ese color forma la línea divisoria).
- Exportar en PNG o JPG con calidad ≥ 90.

**Zona segura**
- Texto, círculos, etiquetas y emojis: **60 px** de margen en los 4 lados (área útil: x 60–1020, y 60–1290).
- Cuadrícula de perfil de Instagram (recorte 3:4): se pierden unos **34 px por lado** en horizontal. Nada importante debe quedar en x < 34 o x > 1046.
- Las fotos sí pueden llegar al borde (sangrado completo).
- Excepción aceptada: en 3a los textos quedan a 26 px del borde inferior de cada foto (ver ficha 3a).

**Paleta**
| Nombre | Hex | Uso |
|---|---|---|
| negro | `#000000` | fondos, contornos, anillos |
| blanco | `#FFFFFF` | texto principal, divisores, halo del círculo 1a |
| rojo | `#E4002B` | barras, franjas, borde del título |
| amarillo | `#FFD400` | palabra resaltada, borde de círculos 5a, etiqueta 1 |

**Tipografías** (ambas son de Google Fonts y están disponibles en Canva)
- **Anton**, Regular (esta fuente solo tiene un peso, y ya es gruesa): titulares, frases, etiquetas, "VS".
- **Oswald**, Bold 700: subtítulos.
- Todo el texto va en MAYÚSCULAS.

**Fotos**
- Todas se colocan en modo *cover*: llenan el marco y se recorta lo que sobra.
- **Encuadre del rostro**: el bot debe recortar cada foto **antes** de enviarla, con el rostro del protagonista en el punto focal indicado en cada ficha. Canva autofill siempre centra la imagen en el marco y no permite elegir el punto focal.
- Resolución mínima recomendada de la foto recortada: igual al tamaño del marco (por ejemplo, 536×1350 para 2a). Los círculos con zoom a partir de capturas pequeñas se ven pixelados.

**Convención de nombres de campos**
- Fotos rectangulares: `foto_1`, `foto_2`, `foto_3`, `foto_4`
- Fotos circulares: `foto_circulo_1`, `foto_circulo_2`, `foto_circulo_3`
- Foto de fondo desenfocada: `foto_fondo`
- Textos: `texto_titular`, `texto_resaltado`, `texto_subtitulo`, `texto_1`, `texto_2`
- Etiquetas: `etiqueta_circulo_1`, `etiqueta_circulo_2`
- Emojis: `emoji_1`, `emoji_2`

**Palabra resaltada en amarillo**
En HTML una misma frase mezcla blanco y amarillo. Canva autofill reemplaza todo el texto de un campo y aplica un solo estilo. Por eso, en Canva la parte amarilla va en **un campo aparte** (`texto_resaltado`), en su propia línea (ver fichas 2a y 4a).

---

## 1a — Detalle + Reacciones

Es el estilo más flexible: acepta 2 o 3 fotos grandes, un círculo en posición variable y énfasis con emojis, flecha o nada.

### Lienzo
- 1080×1350, fondo `#000000`.
- Separación de 6 px entre fotos (se ve el fondo negro).

### Capas en modo 2 fotos, de atrás hacia adelante
| # | Capa | Tipo | x | y | w × h | Detalle |
|---|---|---|---|---|---|---|
| 0 | fondo | forma | 0 | 0 | 1080×1350 | `#000000` |
| 1 | `foto_1` | foto rect. | 0 | 0 | 1080×672 | cover, sin tratamiento; escena principal |
| 2 | `foto_2` | foto rect. | 0 | 678 | 1080×672 | cover; reacción o segunda escena |
| 3 | `foto_circulo_1` | círculo | según posición | según posición | 420×420 (exterior) | foto de 400 Ø, halo 10 px `#FFFFFF`, sombra 0/12/40 px `rgba(0,0,0,.6)` |
| 4 | `emoji_1` | emoji | relativo al círculo | | 120 px | rotación −14°, sombra 0/6/8 `rgba(0,0,0,.6)` |
| 5 | `emoji_2` | emoji | relativo al círculo | | 110 px | rotación +12°, misma sombra |
| 4-alt | flecha | ícono PNG | relativo al círculo | | 260×200 | reemplaza a los emojis (ver abajo) |

### Capas en modo 3 fotos
| Capa | x | y | w × h |
|---|---|---|---|
| `foto_1` | 0 | 0 | 1080×446 |
| `foto_2` | 0 | 452 | 1080×446 |
| `foto_3` | 0 | 904 | 1080×446 |

El círculo y el énfasis se mantienen igual. En modo 3 fotos conviene usar un círculo más chico: exterior de 360 (foto de 340 Ø, halo de 10).

### Posiciones del círculo (`posicion_circulo`)
Coordenada de la esquina superior izquierda del círculo exterior de 420 px. Todas respetan la zona segura de 60 px.
| id | x | y | Cuándo usarla |
|---|---|---|---|
| `sup_izq` | 60 | 60 | el detalle está abajo a la derecha de foto_1 |
| `sup_der` | 600 | 60 | el detalle está abajo a la izquierda de foto_1 |
| `costura_izq` | 60 | 465 | encima de la unión entre foto_1 y foto_2, a la izquierda |
| `costura_der` | 600 | 450 | encima de la unión, a la derecha (**usada en el post Pascal vs Kevyn**) |
| `centro` | 330 | 465 | centrado, encima de la unión |
| `inf_izq` | 60 | 870 | sobre foto_2, abajo a la izquierda |
| `inf_der` | 600 | 870 | sobre foto_2, abajo a la derecha |

Regla: el círculo no debe tapar el rostro principal de `foto_1` ni de `foto_2`. El bot elige la posición opuesta al rostro.

### Énfasis (`enfasis`: `emojis` | `flecha` | `ninguno`)
Nunca van emojis y flecha a la vez.
- **emojis**: posiciones medidas desde el centro del círculo (cx, cy).
  - `emoji_1`: centro en (cx − 243, cy − 151). Si el círculo está a la izquierda, se usa (cx + 243, cy − 151).
  - `emoji_2`: centro en (cx + 167, cy + 221). Si el círculo está a la derecha y se sale del lienzo, se usa (cx − 167, cy + 221).
  - Emojis usados en Pascal vs Kevyn: 😡 y 💥. Otras opciones según el tono: 👀 😱 😳 🔥 🤯.
- **flecha**: curva, trazo `#E4002B` de 28 px con contorno `#FFFFFF` de 9 px (ancho total de 46 px) y punta triangular roja con contorno blanco.
  - Caja de 260×200. Su punta queda a unos 20 px del borde del círculo, del lado opuesto a la zona segura.
  - Se exporta como PNG en 2 versiones: apuntando a la derecha y reflejada horizontalmente.

### Campos variables
`foto_1`, `foto_2`, `foto_3` (solo en modo 3 fotos), `foto_circulo_1`, `emoji_1`, `emoji_2`. Parámetros de armado: `modo_fotos` (2|3), `posicion_circulo`, `enfasis`.

### Variantes según cuántas fotos llegan
| Llegan | Armado |
|---|---|
| 2 | foto_1 + foto_2 rect.; el círculo recorta un detalle de foto_1 o foto_2 |
| 3 | opción A: 2 rect. + la 3.ª como círculo (**preferida**). Opción B: 3 rect. + círculo recortado de una de ellas |
| 4 | 3 rect. + la 4.ª como círculo |
| 5 | 3 rect. + 1 círculo; la 5.ª se descarta. Máximo: 3 rect. + 1 círculo |

```json
{
  "estilo": "1a_detalle_reacciones",
  "lienzo": { "w": 1080, "h": 1350, "fondo": "#000000", "gap": 6 },
  "layouts": {
    "2_fotos": [
      { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 1080, "h": 672, "fit": "cover" },
      { "campo": "foto_2", "tipo": "foto_rect", "x": 0, "y": 678, "w": 1080, "h": 672, "fit": "cover" }
    ],
    "3_fotos": [
      { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 1080, "h": 446, "fit": "cover" },
      { "campo": "foto_2", "tipo": "foto_rect", "x": 0, "y": 452, "w": 1080, "h": 446, "fit": "cover" },
      { "campo": "foto_3", "tipo": "foto_rect", "x": 0, "y": 904, "w": 1080, "h": 446, "fit": "cover" }
    ]
  },
  "circulo": {
    "campo": "foto_circulo_1", "diametro_foto": 400, "diametro_exterior": 420,
    "diametro_foto_modo3": 340, "diametro_exterior_modo3": 360,
    "borde": { "grosor": 10, "color": "#FFFFFF" },
    "sombra": { "x": 0, "y": 12, "blur": 40, "color": "rgba(0,0,0,0.6)" },
    "posiciones": {
      "sup_izq": [60, 60], "sup_der": [600, 60],
      "costura_izq": [60, 465], "costura_der": [600, 450], "centro": [330, 465],
      "inf_izq": [60, 870], "inf_der": [600, 870]
    }
  },
  "enfasis": {
    "opciones": ["emojis", "flecha", "ninguno"],
    "emojis": [
      { "campo": "emoji_1", "size": 120, "rot": -14, "offset_desde_centro_circulo": [-243, -151] },
      { "campo": "emoji_2", "size": 110, "rot": 12, "offset_desde_centro_circulo": [167, 221] }
    ],
    "emoji_sombra": { "x": 0, "y": 6, "blur": 8, "color": "rgba(0,0,0,0.6)" },
    "flecha": { "w": 260, "h": 200, "trazo": "#E4002B", "grosor": 28, "contorno": "#FFFFFF", "contorno_grosor": 9, "variantes": ["der", "izq"] }
  },
  "campos_variables": ["foto_1", "foto_2", "foto_3", "foto_circulo_1", "emoji_1", "emoji_2"],
  "parametros_bot": ["modo_fotos", "posicion_circulo", "enfasis"]
}
```

---

## 2a — Duelo Vertical

### Lienzo
- 1080×1350, fondo `#FFFFFF`.
- El fondo se ve como una línea divisoria blanca de 8 px en x 536–544.

### Capas, de atrás hacia adelante
| # | Capa | Tipo | x | y | w × h | Detalle |
|---|---|---|---|---|---|---|
| 0 | fondo | forma | 0 | 0 | 1080×1350 | `#FFFFFF` |
| 1 | `foto_1` | foto rect. | 0 | 0 | 536×1350 | cover; rostro centrado en x≈268, y≈30–42 % de la altura |
| 2 | `foto_2` | foto rect. | 544 | 0 | 536×1350 | igual que foto_1; lo ideal es que el rostro mire hacia el centro |
| 3 | degradado | forma | 0 | 710 | 1080×640 | vertical: `rgba(0,0,0,0)` → `rgba(0,0,0,.92)` al 45 % → `#000000` al 100 % |
| 4 | barra | forma | 360 | 805 | 360×10 | `#E4002B`, sin contorno |
| 5 | `texto_titular` | texto | 50 | 837 | 980 × ~173 por línea | Anton 160, interlineado 1.08, espaciado 2 px, `#FFFFFF`, centrado, sin contorno |
| 5b | `texto_resaltado` | texto | 50 | justo debajo del titular | 980 | Anton 160, `#FFD400`; en Canva va en campo aparte, en su propia línea |
| 6 | `texto_subtitulo` | texto | 50 | 1205 | 980×65 | Oswald Bold 44, `#FFFFFF`, centrado, 1 línea; puede terminar en emoji (💥) |

- Titular: máximo unos 12 caracteres por línea y 2 líneas en total (titular + resaltado).
- Ejemplo: `texto_titular` = "¡PAGA TU" y `texto_resaltado` = "DEUDA!".
- El texto termina en y≈1270, dentro de la zona segura.

### Campos variables
`foto_1`, `foto_2`, `texto_titular`, `texto_resaltado`, `texto_subtitulo`.

### Variantes según cuántas fotos llegan
| Llegan | Armado |
|---|---|
| 2 | foto_1 = protagonista A, foto_2 = protagonista B |
| 3–5 | se eligen las 2 fotos con los rostros más claros de los 2 protagonistas; el resto se descarta. Este estilo siempre usa 2 fotos |

La frase debe ser corta y contundente, no una descripción.

```json
{
  "estilo": "2a_duelo_vertical",
  "lienzo": { "w": 1080, "h": 1350, "fondo": "#FFFFFF" },
  "capas": [
    { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 536, "h": 1350, "fit": "cover", "foco_rostro": [0.5, 0.35] },
    { "campo": "foto_2", "tipo": "foto_rect", "x": 544, "y": 0, "w": 536, "h": 1350, "fit": "cover", "foco_rostro": [0.5, 0.35] },
    { "tipo": "degradado", "x": 0, "y": 710, "w": 1080, "h": 640, "stops": [[0, "rgba(0,0,0,0)"], [0.45, "rgba(0,0,0,0.92)"], [1, "#000000"]] },
    { "tipo": "forma", "x": 360, "y": 805, "w": 360, "h": 10, "color": "#E4002B" },
    { "campo": "texto_titular", "tipo": "texto", "x": 50, "y": 837, "w": 980, "fuente": "Anton", "size": 160, "line_height": 1.08, "tracking": 2, "color": "#FFFFFF", "align": "center", "uppercase": true },
    { "campo": "texto_resaltado", "tipo": "texto", "x": 50, "y": "debajo_de_titular", "w": 980, "fuente": "Anton", "size": 160, "line_height": 1.08, "tracking": 2, "color": "#FFD400", "align": "center", "uppercase": true },
    { "campo": "texto_subtitulo", "tipo": "texto", "x": 50, "y": 1205, "w": 980, "h": 65, "fuente": "Oswald", "peso": 700, "size": 44, "color": "#FFFFFF", "align": "center", "uppercase": true }
  ],
  "campos_variables": ["foto_1", "foto_2", "texto_titular", "texto_resaltado", "texto_subtitulo"]
}
```

---

## 3a — Clásico renovado

### Lienzo
- 1080×1350, fondo `#FFFFFF`.
- El fondo se ve como un divisor blanco de 8 px en y 671–679.

### Capas, de atrás hacia adelante
| # | Capa | Tipo | x | y | w × h | Detalle |
|---|---|---|---|---|---|---|
| 0 | fondo | forma | 0 | 0 | 1080×1350 | `#FFFFFF` |
| 1 | `foto_1` | foto rect. | 0 | 0 | 1080×671 | cover; rostro en el tercio superior (el texto tapa el tercio inferior) |
| 2 | degradado_1 | forma | 0 | 411 | 1080×260 | `rgba(0,0,0,0)` → `rgba(0,0,0,.75)` |
| 3 | `texto_1` | texto | 30 | anclado abajo en y=645 | 1020 × máx. 146 (2 líneas) | Anton 70, interlineado 1.04, `#FFFFFF`, **contorno exterior 8 px `#000000`**, centrado |
| 4 | `emoji_1` | emoji | al final de texto_1 | | 70 px | en Canva va como campo aparte o dentro del texto |
| 5 | `foto_2` | foto rect. | 0 | 679 | 1080×671 | cover |
| 6 | degradado_2 | forma | 0 | 1090 | 1080×260 | igual que degradado_1 |
| 7 | `texto_2` | texto | 30 | anclado abajo en y=1324 | 1020 × máx. 146 | igual que texto_1 |
| 8 | `emoji_2` | emoji | al final de texto_2 | | 70 px | |

- Palabra resaltada (opcional): `#FFD400`. En Canva, texto_1 y texto_2 van en un solo color, así que el resaltado se omite o va en una 2.ª línea como campo aparte.
- Largo máximo por texto: unos 40 caracteres (2 líneas).
- Los emojis deben ir con el tono de la frase: 💸😡 para un reclamo, 🤒 para una excusa, ❌/✅ para un antes/después.
- Formato de las frases: `NOMBRE: FRASE`.
- El borde inferior de texto_2 queda a 26 px del borde del lienzo. Es un margen aceptado para este estilo, igual que en el formato actual. Si el destino recorta, subir los textos 34 px.

### Campos variables
`foto_1`, `foto_2`, `texto_1`, `texto_2`, `emoji_1`, `emoji_2`.

### Variantes según cuántas fotos llegan
| Llegan | Armado |
|---|---|
| 2 | foto + frase por panel |
| 3–5 | se usan las 2 fotos que corresponden a cada frase; el resto se descarta. Siempre 2 paneles |

```json
{
  "estilo": "3a_clasico_renovado",
  "lienzo": { "w": 1080, "h": 1350, "fondo": "#FFFFFF" },
  "capas": [
    { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 1080, "h": 671, "fit": "cover", "foco_rostro": [0.5, 0.3] },
    { "tipo": "degradado", "x": 0, "y": 411, "w": 1080, "h": 260, "stops": [[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,0.75)"]] },
    { "campo": "texto_1", "tipo": "texto", "x": 30, "y_base": 645, "w": 1020, "max_lineas": 2, "fuente": "Anton", "size": 70, "line_height": 1.04, "color": "#FFFFFF", "contorno": { "grosor": 8, "color": "#000000" }, "align": "center", "uppercase": true },
    { "campo": "emoji_1", "tipo": "emoji", "size": 70, "pos": "final_de_texto_1" },
    { "campo": "foto_2", "tipo": "foto_rect", "x": 0, "y": 679, "w": 1080, "h": 671, "fit": "cover", "foco_rostro": [0.5, 0.3] },
    { "tipo": "degradado", "x": 0, "y": 1090, "w": 1080, "h": 260, "stops": [[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,0.75)"]] },
    { "campo": "texto_2", "tipo": "texto", "x": 30, "y_base": 1324, "w": 1020, "max_lineas": 2, "fuente": "Anton", "size": 70, "line_height": 1.04, "color": "#FFFFFF", "contorno": { "grosor": 8, "color": "#000000" }, "align": "center", "uppercase": true },
    { "campo": "emoji_2", "tipo": "emoji", "size": 70, "pos": "final_de_texto_2" }
  ],
  "color_resaltado": "#FFD400",
  "campos_variables": ["foto_1", "foto_2", "texto_1", "texto_2", "emoji_1", "emoji_2"]
}
```

---

## 4a — Mosaico invertido

### Lienzo
- 1080×1350, fondo `#000000`.
- Separación de 6 px entre fotos.

### Capas en modo 3 fotos (base), de atrás hacia adelante
| # | Capa | Tipo | x | y | w × h | Detalle |
|---|---|---|---|---|---|---|
| 0 | fondo | forma | 0 | 0 | 1080×1350 | `#000000` |
| 1 | `foto_1` | foto rect. | 0 | 0 | 560×1166 | vertical, protagonista; rostro en x≈280, y≈35 % |
| 2 | `foto_2` | foto rect. | 566 | 0 | 514×580 | cover |
| 3 | `foto_3` | foto rect. | 566 | 586 | 514×580 | cover |
| 4 | barra_titulo | forma | 0 | 1172 | 1080×178 | `#000000` con franja superior de 8 px `#E4002B` (y 1172–1180) |
| 5 | `texto_titular` | texto | 60 | centrado en y≈1265 | 960×~110 | Anton 88, espaciado 1 px, `#FFFFFF`, centrado, 1 línea |
| 5b | `texto_resaltado` | texto | a continuación del titular | | | Anton 88, `#FFD400` |
| 6 | `emoji_1` | emoji | al final del titular | | 88 px | en Pascal vs Kevyn: 😤 |

- Titular: máximo unos 22 caracteres para que entre en 1 línea a 88 px. Si es más largo, bajar a 72 px.
- En Canva, como el resaltado va en otro campo, conviene la estructura "titular blanco + nombre amarillo": `texto_titular` = "¡A TRABAJAR," y `texto_resaltado` = "KEVYN!", en 2 cajas alineadas.
  - Alternativa más simple: todo el titular blanco en una sola caja.
- **Título opcional**: sin título, las fotos ocupan 1350 de alto (foto_1 560×1350; foto_2 y foto_3 de 514×672).

### Campos variables
`foto_1`, `foto_2`, `foto_3`, `foto_4` (solo en modo 4 fotos), `texto_titular`, `texto_resaltado`, `emoji_1`. Parámetros: `modo_fotos`, `con_titulo`.

### Variantes según cuántas fotos llegan
| Llegan | Armado |
|---|---|
| 2 | no aplica; usar 2a o 3a |
| 3 | 1 vertical + 2 horizontales (base) |
| 4 | 1 vertical + 3 horizontales: columna derecha x 566, w 514, altos 385/384/385 en y 0, 391, 781 |
| 5 | 1 + 3; la 5.ª se descarta |

Orientación normal (espejo del invertido): foto grande horizontal arriba, x0 y0 1080×640, y 3 verticales abajo (356×520 en x 0, 362 y 724, y 646) + barra de título. Queda documentada como variante `4a_normal`.

```json
{
  "estilo": "4a_mosaico_invertido",
  "lienzo": { "w": 1080, "h": 1350, "fondo": "#000000", "gap": 6 },
  "layouts": {
    "3_fotos_con_titulo": [
      { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 560, "h": 1166, "fit": "cover", "foco_rostro": [0.5, 0.35] },
      { "campo": "foto_2", "tipo": "foto_rect", "x": 566, "y": 0, "w": 514, "h": 580, "fit": "cover" },
      { "campo": "foto_3", "tipo": "foto_rect", "x": 566, "y": 586, "w": 514, "h": 580, "fit": "cover" }
    ],
    "4_fotos_con_titulo": [
      { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 560, "h": 1166 },
      { "campo": "foto_2", "tipo": "foto_rect", "x": 566, "y": 0, "w": 514, "h": 385 },
      { "campo": "foto_3", "tipo": "foto_rect", "x": 566, "y": 391, "w": 514, "h": 384 },
      { "campo": "foto_4", "tipo": "foto_rect", "x": 566, "y": 781, "w": 514, "h": 385 }
    ],
    "3_fotos_sin_titulo": [
      { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 560, "h": 1350 },
      { "campo": "foto_2", "tipo": "foto_rect", "x": 566, "y": 0, "w": 514, "h": 672 },
      { "campo": "foto_3", "tipo": "foto_rect", "x": 566, "y": 678, "w": 514, "h": 672 }
    ]
  },
  "barra_titulo": { "x": 0, "y": 1172, "w": 1080, "h": 178, "color": "#000000", "borde_superior": { "grosor": 8, "color": "#E4002B" } },
  "textos": [
    { "campo": "texto_titular", "fuente": "Anton", "size": 88, "size_min": 72, "tracking": 1, "color": "#FFFFFF", "align": "center", "centro_y": 1265, "max_caracteres": 22 },
    { "campo": "texto_resaltado", "fuente": "Anton", "size": 88, "color": "#FFD400" },
    { "campo": "emoji_1", "tipo": "emoji", "size": 88, "pos": "final_de_titular" }
  ],
  "campos_variables": ["foto_1", "foto_2", "foto_3", "foto_4", "texto_titular", "texto_resaltado", "emoji_1"],
  "parametros_bot": ["modo_fotos", "con_titulo"]
}
```

---

## 5a — Bloque con Círculos

### Lienzo
- 1080×1350, fondo `#000000`.
- Separación de 6 px entre la mitad superior y la inferior.

### Capas en modo escena + 2 círculos, de atrás hacia adelante
| # | Capa | Tipo | x | y | w × h | Detalle |
|---|---|---|---|---|---|---|
| 0 | fondo | forma | 0 | 0 | 1080×1350 | `#000000` |
| 1 | `foto_1` | foto rect. | 0 | 0 | 1080×672 | escena donde se ven los protagonistas |
| 2 | `foto_fondo` | foto rect. | 0 | 678 | 1080×672 | **desenfoque 14 px** (en Canva, Desenfocar ≈ 30), saturación +30 %, escala 115 %. Por defecto es la misma imagen que foto_circulo_2 |
| 3 | overlay | forma | 0 | 678 | 1080×672 | degradado vertical `rgba(0,0,0,.55)` → `rgba(0,0,0,.85)` |
| 4 | franja | forma | −100 | 928 | 1280×170 | `#E4002B`, **rotación −6°**, contorno 10 px `#000000`, centro en (540, 1013) |
| 5 | `foto_circulo_1` | círculo | 42 | 782 | 428×428 | foto de 400 Ø; **anillo interior** 14 px `#FFD400`; **anillo exterior** 8 px `#000000` (diámetro visual total de 444); sombra 0/18/50 `rgba(0,0,0,.7)` |
| 6 | `etiqueta_circulo_1` | texto con fondo | centrada en x=256 | 1171 | ~215×81 | caja `#FFD400`, texto `#000000` Anton 52, espaciado 2 px, relleno 10/28/8/28 px, **rotación −3°**, sombra 0/8/20 `rgba(0,0,0,.5)`; se superpone 34 px sobre el círculo |
| 7 | "VS" | texto fijo | 473 | 923 | 134×142 | Anton 130, `#FFFFFF`, **contorno 10 px `#000000`**, **rotación −6°**, centro en (540, 994) |
| 8 | `foto_circulo_2` | círculo | 611 | 782 | 428×428 | igual que el círculo 1 |
| 9 | `etiqueta_circulo_2` | texto con fondo | centrada en x=825 | 1171 | ~189×80 | caja `#FFFFFF`, texto `#000000`; el resto igual que la etiqueta 1 |

- Etiquetas: nombre en mayúsculas, máximo 10 caracteres.
- Encuadre de los círculos: rostro centrado, ocupando aproximadamente el 55 % del diámetro.

### Campos variables
`foto_1`, `foto_fondo`, `foto_circulo_1`, `foto_circulo_2`, `foto_circulo_3` (solo en modo 5 fotos), `etiqueta_circulo_1`, `etiqueta_circulo_2`, `etiqueta_circulo_3`.
"VS", la franja y los colores son fijos.

### Variantes según cuántas fotos llegan
| Llegan | Armado |
|---|---|
| 2 | escena + 2 círculos recortados de las mismas 2 fotos; foto_fondo = foto_circulo_2 |
| 3 | escena + 2 círculos (base) |
| 4 | arriba 2 rectángulos iguales (`foto_1` x0 y `foto_2` x543, ambos de 537×672) + abajo los 2 círculos (base) |
| 5 | arriba 2 rectángulos + abajo **3 círculos** (foto de 290 Ø, anillo de 12 `#FFD400` + 6 `#000000`, centros x 190/540/890, y 1000), **sin "VS"**, etiquetas Anton 40 debajo de cada círculo; franja igual |

```json
{
  "estilo": "5a_bloque_circulos",
  "lienzo": { "w": 1080, "h": 1350, "fondo": "#000000", "gap": 6 },
  "capas": [
    { "campo": "foto_1", "tipo": "foto_rect", "x": 0, "y": 0, "w": 1080, "h": 672, "fit": "cover" },
    { "campo": "foto_fondo", "tipo": "foto_rect", "x": 0, "y": 678, "w": 1080, "h": 672, "fit": "cover", "blur_px": 14, "saturacion": 1.3, "escala": 1.15, "default": "foto_circulo_2" },
    { "tipo": "degradado", "x": 0, "y": 678, "w": 1080, "h": 672, "stops": [[0, "rgba(0,0,0,0.55)"], [1, "rgba(0,0,0,0.85)"]] },
    { "tipo": "franja", "x": -100, "y": 928, "w": 1280, "h": 170, "color": "#E4002B", "rot": -6, "contorno": { "grosor": 10, "color": "#000000" } },
    { "campo": "foto_circulo_1", "tipo": "circulo", "x": 42, "y": 782, "diametro_foto": 400, "anillos": [{ "grosor": 14, "color": "#FFD400" }, { "grosor": 8, "color": "#000000" }], "sombra": { "x": 0, "y": 18, "blur": 50, "color": "rgba(0,0,0,0.7)" } },
    { "campo": "etiqueta_circulo_1", "tipo": "texto_con_fondo", "centro_x": 256, "y": 1171, "fondo": "#FFD400", "color": "#000000", "fuente": "Anton", "size": 52, "tracking": 2, "padding": [10, 28, 8, 28], "rot": -3, "sombra": { "x": 0, "y": 8, "blur": 20, "color": "rgba(0,0,0,0.5)" }, "max_caracteres": 10 },
    { "tipo": "texto_fijo", "texto": "VS", "x": 473, "y": 923, "w": 134, "h": 142, "fuente": "Anton", "size": 130, "color": "#FFFFFF", "contorno": { "grosor": 10, "color": "#000000" }, "rot": -6 },
    { "campo": "foto_circulo_2", "tipo": "circulo", "x": 611, "y": 782, "diametro_foto": 400, "anillos": [{ "grosor": 14, "color": "#FFD400" }, { "grosor": 8, "color": "#000000" }] },
    { "campo": "etiqueta_circulo_2", "tipo": "texto_con_fondo", "centro_x": 825, "y": 1171, "fondo": "#FFFFFF", "color": "#000000", "fuente": "Anton", "size": 52, "tracking": 2, "padding": [10, 28, 8, 28], "rot": -3 }
  ],
  "variante_5_fotos": {
    "rects_superiores": [{ "campo": "foto_1", "x": 0, "y": 0, "w": 537, "h": 672 }, { "campo": "foto_2", "x": 543, "y": 0, "w": 537, "h": 672 }],
    "circulos": { "diametro_foto": 290, "anillos": [{ "grosor": 12, "color": "#FFD400" }, { "grosor": 6, "color": "#000000" }], "centros": [[190, 1000], [540, 1000], [890, 1000]] },
    "vs": false, "etiqueta_size": 40
  },
  "campos_variables": ["foto_1", "foto_2", "foto_fondo", "foto_circulo_1", "foto_circulo_2", "foto_circulo_3", "etiqueta_circulo_1", "etiqueta_circulo_2", "etiqueta_circulo_3"]
}
```

---

## 6. Canva Connect API — viabilidad

**No pude crear las plantillas ni conseguir sus ID.** Desde este proyecto no tengo conexión con tu cuenta de Canva. Tampoco la API permite crear Brand Templates: se crean a mano en el editor de Canva y la API solo las lista y las rellena.

Lo siguiente es conocimiento general de la API. Conviene confirmarlo con la documentación oficial de Canva Connect antes de desarrollar.

**Requisitos**
- Las APIs de Brand Templates y Autofill están limitadas a cuentas **Canva Enterprise**. Con Pro o Teams no se puede rellenar por API.
- Crear cada plantilla en el editor.
- Marcar cada campo variable con la app **"Autocompletar datos" (Data autofill)**, usando exactamente los nombres de esta ficha.
- Publicarla como Brand Template.

**Flujo del bot**
1. `GET /v1/brand-templates` para obtener los ID.
2. `GET /v1/brand-templates/{id}/dataset` para verificar los nombres de los campos.
3. Subir cada foto ya recortada con `POST /v1/asset-uploads`, que devuelve un `asset_id`.
4. `POST /v1/autofills` con `brand_template_id` y `data`: los textos como `{type:"text"}` y las fotos como `{type:"image", asset_id}`.
5. Consultar el job y exportar con `POST /v1/exports` (PNG).

**Qué se puede hacer con autofill y qué no**
| Estilo | ¿Sirve como Brand Template? | Limitaciones |
|---|---|---|
| 2a Duelo | Sí, 1 plantilla | el resaltado amarillo va en un campo aparte |
| 3a Clásico | Sí, 1 plantilla | un solo color por frase; contorno negro con efecto de texto de Canva (verificar que se vea igual) |
| 4a Mosaico | Sí, 3 plantillas (3 fotos con título / 4 fotos con título / sin título) | el titular no se reduce solo si es largo: el bot debe controlar los caracteres |
| 5a Círculos | Sí, 3 plantillas (2–3 fotos / 4 fotos / 5 fotos) | el desenfoque queda fijo en la plantilla y se aplica a la foto que entra |
| 1a Detalle | Sí, pero **una plantilla por combinación**, porque autofill no mueve elementos ni los oculta | 2 modos × 7 posiciones × 3 énfasis = 42 plantillas. Recomendado: reducir a 2 modos × 4 posiciones (`sup_der`, `costura_izq`, `costura_der`, `inf_izq`) × 2 énfasis (emojis / ninguno) = **16 plantillas** |

**Qué hace el bot fuera de Canva, en todos los estilos**
- Recortar las fotos con el rostro en el punto focal.
- Contar caracteres.
- Elegir el estilo y la variante (ID de plantilla) según la nota y la cantidad de fotos.

**Alternativa sin Canva**
Estas 5 plantillas ya existen como HTML con medidas exactas. Un bot puede rellenarlas y exportarlas a PNG con un navegador sin interfaz (Puppeteer o Playwright). Así se obtiene lo que Canva autofill no permite: círculo en cualquier posición, recorte por rostro, palabra resaltada dentro de la frase y títulos que se reducen solos. Además no requiere plan Enterprise.

**Nota sobre el límite de 16 plantillas del estilo 1a**: ese límite es específico de Canva autofill, que no puede mover elementos por API. Con el camino HTML + Playwright no aplica: el círculo se puede posicionar en cualquier coordenada real dentro de la zona segura, no solo en las 7 posiciones catalogadas. La lista de posiciones de la ficha 1a queda como catálogo de referencia (las más probadas), no como límite técnico.

---

## 7. Catálogo de variantes (para variedad visual)

Las fichas 2a-5a quedaron documentadas con un solo esquema de color y una sola disposición porque así salió el ejemplo de Pascal vs Kevyn. Para que el bot no publique siempre con la misma cara, cada estilo elige entre estas variantes al momento de armar el post (por regla simple según el tono de la nota, o rotando para no repetir la misma combinación dos veces seguidas).

### 7.1 Paletas de acento

Reemplazan el rojo `#E4002B` / amarillo `#FFD400` fijo en cualquier capa marcada como "acento" (barras, franjas, bordes, texto resaltado, anillos de círculo). El negro y el blanco de fondo/texto no cambian.

| id | Acento 1 | Acento 2 | Tono / cuándo usarla |
|---|---|---|---|
| `rojo_amarillo` | `#E4002B` | `#FFD400` | confrontación, pelea, tensión (la usada en Pascal vs Kevyn) |
| `azul_blanco` | `#0B5FFF` | `#FFFFFF` | informativo, anuncio neutral |
| `violeta_rosa` | `#8B2FC9` | `#FF4FA3` | chisme, drama, romance |
| `verde_lima` | `#00A859` | `#C6FF00` | triunfo, buena noticia, salvada |
| `naranja_negro` | `#FF6B00` | `#000000` | alerta, urgencia, última hora |

Regla de armado: Acento 1 va en franjas/barras/fondos de etiqueta; Acento 2 va en el texto resaltado o el segundo anillo del círculo. `ask_claude` puede devolver qué paleta conviene junto con el resto de sus decisiones (mismo mecanismo que ya usa para elegir estilo).

### 7.1b Pares de color de texto (base + resaltado)

En casi todos los posts, la palabra resaltada es lo más importante de la frase. El par de colores del texto se elige aparte de la paleta de acento, y siempre dentro de combinaciones que armonizan (nunca dos colores cálidos saturados iguales, nunca resaltado de menor contraste que el texto base).

| id | Texto base | Resaltado | Contorno / sombra |
|---|---|---|---|
| `blanco_amarillo` | `#FFFFFF` | `#FFD400` | negro (base, la del ejemplo) |
| `blanco_rojo` | `#FFFFFF` | `#FF2D3D` | negro |
| `amarillo_rojo` | `#FFD400` | `#FF2D3D` | negro grueso (8-10 px) obligatorio |
| `amarillo_blanco` | `#FFD400` | `#FFFFFF` | negro |
| `blanco_cian` | `#FFFFFF` | `#00E5FF` | negro |
| `blanco_rosa` | `#FFFFFF` | `#FF4FA3` | negro |
| `negro_rojo` | `#000000` | `#E4002B` | blanco (solo sobre fondos claros o cajas blancas) |

**Regla de elección según la foto** (la hace el bot antes de renderizar):
1. Toma la zona de la foto que queda detrás del texto (la franja donde va el titular o la frase).
2. Calcula su color dominante y su brillo promedio.
3. Descarta los pares cuyo **resaltado** tenga un tono parecido al dominante (diferencia de matiz < 35°), para que la palabra clave no se pierda con la ropa o el fondo. Ejemplo: camisa roja a cuadros detrás del texto → se descartan `blanco_rojo` y `amarillo_rojo`.
4. Descarta los pares cuyo texto base no llegue a contraste ≥ 4.5:1 contra el brillo de esa zona (considerando el degradado oscuro que ya lleva cada estilo).
5. Entre los que quedan, elige el que corresponda al tono de la nota o, si no hay preferencia, rota para no repetir el del post anterior.

El mismo chequeo se aplica a las etiquetas del 5a y a los anillos de los círculos: si el borde amarillo se confunde con el fondo de la foto, se usa el acento de otra paleta.

### 7.2 Catálogo de emojis por tono

| Tono | Emojis |
|---|---|
| pelea / tensión | 😡 🔥 💥 👊 |
| sorpresa / shock | 😱 😳 👀 🤯 |
| tristeza / lástima | 🥺 💔 😢 😔 |
| triunfo / alegría | 🎉 ✅ 😍 🙌 |
| sospecha / intriga | 🤔 👁️ ❓ 🕵️ |
| vergüenza / incomodidad | 😬 🙈 😅 |

Se elige el emoji dentro del tono ya detectado por `ask_claude` (el mismo que decide el caption), no al azar entre todos.

### 7.3 Variantes de disposición por estilo

- **1a Detalle+Reacciones**: las 7 posiciones de círculo de la ficha son el catálogo base; con HTML se puede usar cualquier x,y dentro de la zona segura si el rostro a evitar cae en un punto intermedio. Énfasis: `emojis` | `flecha` | `ninguno` (ya documentado).
- **2a Duelo Vertical**: qué protagonista va a la izquierda (normalmente el que "gana" o inicia la frase) es variable, no fijo en foto_1. Variante de fondo: `#FFFFFF` (base) o `#000000` (alternativa oscura, con el degradado invertido a `rgba(255,255,255,x)`).
- **3a Clásico renovado**: el orden de los 2 paneles (quién va arriba) es variable según quién habla primero en la frase. Variante de fondo: blanco (base) o negro.
- **4a Mosaico invertido**: alternar entre `4a_invertido` (grande a la izquierda) y `4a_normal` (grande arriba, espejado) — ya documentado como variante, usarlo activamente y no solo como caso de respaldo.
- **5a Bloque con Círculos**: franja diagonal (base, −6°) o franja vertical simple sin rotación como variante B; con o sin "VS" (el modo 5 fotos ya va sin "VS", se puede usar esa versión también con 3 fotos si se quiere variar).

### 7.4 Parámetro de armado

Se agrega `paleta` a los parámetros que ya tenía cada estilo (`modo_fotos`, `posicion_circulo`, `enfasis`, `con_titulo`), así el bot puede registrar y rotar la combinación elegida para no repetir la misma paleta+disposición en publicaciones seguidas.

---

## 8. Reconocimiento de participantes (quién es quién)

Objetivo: que el bot sepa quién aparece en cada foto (Pascal, Kevyn, etc.) para elegir bien qué cara va en el círculo, qué foto va a cada lado del duelo y qué etiqueta lleva cada círculo.

**Cómo funciona** (no es "entrenar" un modelo, es registrar caras de referencia):
1. **Registro por Telegram**: se manda al bot `/participante Pascal` + 3 a 8 fotos donde se le vea la cara (de frente, de perfil, con y sin gorra, distinta luz). El bot detecta el rostro en cada foto, calcula su "huella facial" (un vector numérico) y la guarda con el nombre.
2. **Al llegar un post**: el bot detecta todas las caras de cada foto, compara cada una contra las huellas guardadas y le pone nombre a las que se parecen lo suficiente. Las que no reconoce quedan como "desconocido".
3. **Uso en el diseño**:
   - Círculos (1a, 5a): recorta la cara de la persona que menciona la nota, centrada y ocupando ~55 % del diámetro.
   - Duelo (2a): pone a cada protagonista en su mitad, con el recorte centrado en su cara.
   - Etiquetas (5a): el nombre sale del reconocimiento, no hay que escribirlo.
   - Reacción: entre varias caras de la misma persona, se elige la más expresiva (esa elección la hace el mismo paso de IA que ya lee la nota, mirando los recortes).
4. **Corrección**: si en el preview de Telegram una cara salió mal, se responde `Esa no es Pascal` / `Pascal es el de la izquierda`, se rehace el diseño, y esa foto se suma como referencia nueva (así se va afinando con el uso).

**Tecnología propuesta**: OpenCV (detector YuNet + reconocedor SFace). Son modelos chicos (~40 MB en total), corren en CPU dentro de GitHub Actions en menos de un segundo por foto y no requieren servicios externos ni cuentas pagas.

**Límites a tener en cuenta**:
- Capturas pequeñas o borrosas (la cara de menos de ~60 px) fallan seguido. Con capturas más cercanas acierta mucho más.
- Perfil muy marcado, lentes oscuros, manos tapando la cara o gorra muy baja bajan la precisión.
- Gente parecida entre sí (mismo corte de pelo, barba, gorra) puede confundirse: por eso conviene 5+ fotos de referencia por persona y umbral de confianza. Si la confianza es baja, el bot lo dice en el preview en vez de adivinar.
- Cambios de temporada/look (corte de pelo, barba nueva) requieren sumar fotos nuevas.

**Privacidad**: las huellas faciales son datos biométricos. No se guardan en el repositorio público: van cifradas, igual que los otros secretos del bot, o en un almacenamiento privado. Se borran con `/olvidar Pascal` cuando el participante sale del programa.
