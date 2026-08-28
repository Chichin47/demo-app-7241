#!/usr/bin/env python3
"""Une dos copias del estado en una sola, sin perder nada.

Motivo: un turno de Actions puede arrancar "clavado" en un commit viejo
(las corridas programadas se crean con la foto del repositorio de ese
momento y esperan en la cola). Si al guardar pisáramos el archivo remoto
con el nuestro, se perdería lo que publicó el turno anterior y esos posts
se volverían a publicar. Acá se juntan las dos versiones: lo publicado es
la unión de ambas, así nada se repite.

Uso:  python tools/merge.py remoto.b64 local.b64 salida.b64
El remoto puede estar vacío o no existir.
"""
import base64
import json
import sys
import zlib


def leer(ruta):
    try:
        with open(ruta, "rb") as fh:
            crudo = fh.read()
    except OSError:
        return {}
    if not crudo.strip():
        return {}
    try:
        datos = zlib.decompress(base64.b64decode(crudo)).decode("utf-8")
        d = json.loads(datos)
        return d if isinstance(d, dict) else {}
    except Exception as e:  # noqa: BLE001
        print(f"  (no se pudo leer {ruta}: {e})")
        return {}


def j(texto, por_defecto):
    try:
        v = json.loads(texto)
    except Exception:  # noqa: BLE001
        return por_defecto
    return v if isinstance(v, type(por_defecto)) else por_defecto


def volcar(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


# Cuanto más adelantado el estado de un envío, más manda. Así, si en una copia
# ya salió publicado ('done'), esa gana siempre y no se vuelve a publicar; y si
# en una ya elegiste la hora ('scheduled'), no se pierde esa elección.
RANGO = {"awaiting": 0, "pending": 0, "scheduled": 1, "publishing": 2, "done": 3}


def _canon(o):
    """Deja el dato en una forma comparable: claves ordenadas y listas ordenadas."""
    if isinstance(o, dict):
        return {k: _canon(v) for k, v in sorted(o.items())}
    if isinstance(o, list):
        items = [_canon(x) for x in o]
        try:
            items.sort(key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        return items
    return o


def firma(bulto):
    """Resumen comparable del estado, sin depender de cómo esté escrito el texto."""
    salida = {}
    for k, v in (bulto or {}).items():
        if isinstance(v, str):
            try:
                salida[k] = _canon(json.loads(v))
                continue
            except Exception:  # noqa: BLE001
                pass
        salida[k] = _canon(v)
    return json.dumps(salida, sort_keys=True, ensure_ascii=False)


def unir_cola(remoto, local):
    """Une las dos colas de Telegram por clave.

    Gana el que esté más adelantado: si en una copia el envío ya salió
    ('done'), esa manda, para no publicarlo dos veces.
    """
    salida = {}
    for origen in (remoto, local):
        for job in origen.get("jobs", []) or []:
            k = job.get("key")
            if not k:
                continue
            previo = salida.get(k)
            if previo is None:
                salida[k] = job
                continue
            if RANGO.get(job.get("status"), 0) >= RANGO.get(previo.get("status"), 0):
                salida[k] = job
    return {"jobs": list(salida.values())}


def main():
    if len(sys.argv) != 4:
        print("uso: merge.py remoto.b64 local.b64 salida.b64", file=sys.stderr)
        return 2
    remoto, local, salida = sys.argv[1], sys.argv[2], sys.argv[3]

    R = leer(remoto)
    L = leer(local)
    if not L:
        print("Sin estado local; no hay nada que unir.")
        return 1
    if not R:
        print("Sin estado remoto; se guarda el local tal cual.")
        final = dict(L)
    else:
        final = dict(R)
        final.update(L)  # por defecto manda lo local

        # 1) Procesados: la unión de ambos. Nunca se olvida un post.
        #
        # Ojo: este archivo NO guarda solo la lista. También guarda la marca de
        # cuál es la página de origen (`pagina_origen`), y puede guardar más
        # cosas mañana. Antes acá se armaba un diccionario nuevo con la lista y
        # nada más, así que cada vez que se guardaba el estado la marca se
        # borraba sin que se notara: adentro del turno seguía andando (el
        # archivo de trabajo no se toca), pero al empezar el turno siguiente el
        # bot leía el estado del repositorio, no encontraba la marca, la tomaba
        # por un cambio de página y anotaba como "ya vistas" las publicaciones
        # que estaban esperando salir. Una publicación perdida por cada relevo.
        # Por eso ahora se conservan TODAS las claves y solo se reemplaza la
        # lista.
        r_ids = j(R.get("processed_ids.json", "{}"), {})
        l_ids = j(L.get("processed_ids.json", "{}"), {})
        pr = set(r_ids.get("processed", []) or [])
        pl = set(l_ids.get("processed", []) or [])
        unido_ids = dict(r_ids)
        unido_ids.update(l_ids)  # ante la duda manda lo local, igual que arriba
        unido_ids["processed"] = sorted(pr | pl)
        final["processed_ids.json"] = volcar(unido_ids)

        # 2) Publicados: también unión.
        mr = j(R.get("published_map.json", "{}"), {})
        ml = j(L.get("published_map.json", "{}"), {})
        mr.update(ml)
        final["published_map.json"] = volcar(mr)

        # 3) Cola de Telegram.
        final["telegram_queue.json"] = volcar(
            unir_cola(j(R.get("telegram_queue.json", "{}"), {}),
                      j(L.get("telegram_queue.json", "{}"), {}))
        )

        # 4) Relojes: el valor más reciente de cada uno.
        def mayor(nombre, campo):
            a = j(R.get(nombre, "{}"), {}).get(campo) or 0
            b = j(L.get(nombre, "{}"), {}).get(campo) or 0
            base = j(L.get(nombre, "{}"), {}) or j(R.get(nombre, "{}"), {})
            if not isinstance(base, dict):
                return
            base[campo] = max(a, b)
            final[nombre] = volcar(base)

        mayor("telegram_offset.json", "offset")
        mayor("publish_clock.json", "last_publish_ts")
        mayor("selfcheck_clock.json", "proximo")

        pu = len(j(final["processed_ids.json"], {}).get("processed", []))
        print(f"Unido: {pu} post(s) procesados, {len(mr)} publicado(s).")

        # Si lo que hay arriba ya contiene todo lo nuestro, no hay nada que
        # subir. El 3 es la señal para que el guardado ni siquiera haga commit.
        if firma(final) == firma(R):
            print("Sin novedades; no hace falta guardar.")
            return 3

    crudo = json.dumps(final, ensure_ascii=False).encode("utf-8")
    b64 = base64.b64encode(zlib.compress(crudo, 9)).decode("ascii")
    lineas = "\n".join(b64[i:i + 96] for i in range(0, len(b64), 96)) + "\n"
    with open(salida, "w", encoding="utf-8") as fh:
        fh.write(lineas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
