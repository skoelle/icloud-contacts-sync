#!/usr/bin/env bash
# Startet die lokale Demo-App mit SQLite-Backend und Fake-Kontakten.
# Erstellt automatisch ein virtuelles Umfeld, wenn nicht vorhanden.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv-demo"

if [ ! -d "$VENV_DIR" ]; then
    echo "Erstelle virtuelles Umfeld in $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installiere Dependencies ..."
"$VENV_DIR/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

echo "Starte Demo-Server auf http://127.0.0.1:8000"
PYTHONPATH="$SCRIPT_DIR/src" exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/demo.py"
