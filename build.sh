#!/usr/bin/env bash
# =============================================================================
#  build.sh - Build an executable of the NFC Desktop App on Linux / macOS
#
#  Uses PyInstaller to bundle nfc_app.py into a single executable (onefile),
#  then copies the lang/ folder next to the executable (the app reads
#  translations from lang/).
#
#  Usage:
#      chmod +x build.sh
#      ./build.sh
#
#  Output:  dist/nfc_app        (binary)
#           dist/lang/*.json     (translations, placed next to the binary)
# =============================================================================
set -e

APP_NAME="nfc_app"
ENTRY="nfc_app.py"

# Pick python (prefer python3)
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

echo "[*] Python: $($PY --version)"

# Install PyInstaller if missing
if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "[*] Installing PyInstaller..."
    "$PY" -m pip install pyinstaller
fi

# Install runtime dependencies (from requirements.txt - uses environment markers)
if [ -f requirements.txt ]; then
    echo "[*] Installing requirements..."
    "$PY" -m pip install -r requirements.txt
fi

# Clean previous build artifacts
echo "[*] Cleaning previous build..."
rm -rf build dist "${APP_NAME}.spec"

# Build: onefile, windowed (no terminal window)
echo "[*] Building with PyInstaller..."
# Icon per OS: macOS uses .icns, Linux loads icon.png at runtime (window/tray).
ICON_ARG=""
if [ "$(uname)" = "Darwin" ] && [ -f icon.icns ]; then
    ICON_ARG="--icon icon.icns"
elif [ -f icon.png ]; then
    ICON_ARG="--icon icon.png"
fi
"$PY" -m PyInstaller \
    --onefile \
    --windowed \
    $ICON_ARG \
    --name "$APP_NAME" \
    "$ENTRY"

# Copy lang/ next to the binary (the app reads translations from there)
if [ -d lang ]; then
    echo "[*] Copying lang/ into dist/..."
    cp -r lang dist/lang
fi

# Copy icon files next to the binary (the app loads icon.png at runtime)
[ -f icon.png ] && cp icon.png dist/icon.png
[ -f icon.ico ] && cp icon.ico dist/icon.ico
[ -f icon.icns ] && cp icon.icns dist/icon.icns

# Linux: ship a .desktop file + icon so the app shows up in the launcher/dock.
if [ "$(uname)" != "Darwin" ] && [ -f nfc_app.desktop ]; then
    cp nfc_app.desktop dist/nfc_app.desktop
    echo "[*] Linux: dist/nfc_app.desktop provided."
    echo "    To install into the menu:"
    echo "      install -Dm644 icon.png ~/.local/share/icons/nfc_app.png"
    echo "      install -Dm755 dist/nfc_app ~/.local/bin/nfc_app"
    echo "      install -Dm644 dist/nfc_app.desktop ~/.local/share/applications/nfc_app.desktop"
fi

echo ""
echo "[+] Done! Executable: dist/${APP_NAME}"
echo "    Run:  cd dist && ./${APP_NAME}"
echo "    Note (Linux): Input mode needs input access -> you may need  sudo ./${APP_NAME}"
echo "    Note (macOS): grant Accessibility permission to the app to type keys."
