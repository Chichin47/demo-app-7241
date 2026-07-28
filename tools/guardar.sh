#!/usr/bin/env bash
# Guarda el estado en el repositorio SIN pisar lo que haya: lo UNE.
#
# Se usa en dos momentos:
#   * cada ciclo del runner, apenas termina un barrido (así, si el turno se
#     cae de golpe, como mucho se pierden los últimos minutos y no una hora
#     entera de registro, que es lo que hacía que se repitieran posts);
#   * al final del turno, desde el workflow.
#
# No toca la carpeta de trabajo: arma el commit con herramientas internas de
# git (hash-object / read-tree / commit-tree) sobre un índice aparte, así se
# puede guardar en pleno funcionamiento sin cambiarle el código al proceso
# que está corriendo.
set -u

cd "$(dirname "$0")/.." || exit 1
TMP="${RUNNER_TEMP:-/tmp}"

python tools/pack.py pack > /dev/null || exit 1
cp data/store.b64 "$TMP/local.b64" || exit 1

export GIT_AUTHOR_NAME="github-actions[bot]"
export GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

for intento in 1 2 3 4 5; do
  git fetch -q origin +refs/heads/main:refs/remotes/origin/main || true
  git show origin/main:data/store.b64 > "$TMP/remote.b64" 2>/dev/null || : > "$TMP/remote.b64"

  python tools/merge.py "$TMP/remote.b64" "$TMP/local.b64" "$TMP/final.b64"
  rc=$?
  # 3 = lo que hay arriba ya contiene todo lo nuestro; no hay nada que subir.
  [ "$rc" = "3" ] && exit 0
  [ "$rc" = "0" ] || exit 1

  blob=$(git hash-object -w "$TMP/final.b64") || exit 1
  rm -f "$TMP/idx"
  arbol=$(GIT_INDEX_FILE="$TMP/idx" sh -c '
      git read-tree origin/main &&
      git update-index --add --cacheinfo 100644,'"$blob"',data/store.b64 &&
      git write-tree') || exit 1
  commit=$(git commit-tree "$arbol" -p origin/main -m "chore: update data [skip ci]") || exit 1

  if git push -q origin "$commit:main" 2>/dev/null; then
    echo "Estado guardado."
    exit 0
  fi
  echo "Otro turno guardó primero; reintento ($intento)."
  sleep 5
done

echo "No se pudo guardar el estado." >&2
exit 1
