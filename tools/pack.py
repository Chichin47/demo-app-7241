#!/usr/bin/env python3
"""Empaqueta y desempaqueta el estado de trabajo.

El estado (qué se procesó, qué se publicó, la cola pendiente) tiene que
sobrevivir entre turnos, pero guardarlo como JSON suelto deja todo legible
y buscable. Así que se guarda un único archivo codificado en base64:

    python tools/pack.py pack     state/*.json  ->  data/store.b64
    python tools/pack.py unpack   data/store.b64 ->  state/*.json

No es cifrado y no pretende serlo: solo evita que el contenido salga en
búsquedas de texto.
"""
import base64
import json
import sys
import zlib
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ESTADO = BASE / "state"
BLOB = BASE / "data" / "store.b64"


def pack():
    if not ESTADO.is_dir():
        print("No hay carpeta de estado; nada que empaquetar.")
        return 0
    bulto = {}
    for archivo in sorted(ESTADO.glob("*.json")):
        try:
            bulto[archivo.name] = archivo.read_text(encoding="utf-8")
        except Exception as e:
            print(f"No pude leer {archivo.name}: {e}")
    if not bulto:
        print("La carpeta de estado está vacía; nada que empaquetar.")
        return 0
    crudo = json.dumps(bulto, ensure_ascii=False).encode("utf-8")
    comprimido = zlib.compress(crudo, 9)
    texto = base64.b64encode(comprimido).decode("ascii")
    # En líneas de 96 para que el diff de git sea manejable.
    lineas = [texto[i:i + 96] for i in range(0, len(texto), 96)]
    BLOB.parent.mkdir(parents=True, exist_ok=True)
    BLOB.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print(f"Empaquetados {len(bulto)} archivos en {BLOB.name} ({len(texto)} car.).")
    return 0


def unpack():
    if not BLOB.exists():
        print("No hay archivo empaquetado todavía; arranco con estado vacío.")
        ESTADO.mkdir(parents=True, exist_ok=True)
        return 0
    texto = "".join(BLOB.read_text(encoding="utf-8").split())
    if not texto:
        print("El archivo empaquetado está vacío.")
        ESTADO.mkdir(parents=True, exist_ok=True)
        return 0
    try:
        crudo = zlib.decompress(base64.b64decode(texto))
        bulto = json.loads(crudo.decode("utf-8"))
    except Exception as e:
        print(f"El archivo empaquetado no se pudo leer ({e}); arranco vacío.")
        ESTADO.mkdir(parents=True, exist_ok=True)
        return 0
    ESTADO.mkdir(parents=True, exist_ok=True)
    for nombre, contenido in bulto.items():
        # Solo nombres simples: nada de rutas hacia afuera.
        limpio = Path(nombre).name
        if not limpio.endswith(".json"):
            continue
        (ESTADO / limpio).write_text(contenido, encoding="utf-8")
    print(f"Restaurados {len(bulto)} archivos de estado.")
    return 0


def main():
    accion = sys.argv[1] if len(sys.argv) > 1 else ""
    if accion == "pack":
        return pack()
    if accion == "unpack":
        return unpack()
    print("Uso: python tools/pack.py pack|unpack")
    return 2


if __name__ == "__main__":
    sys.exit(main())
