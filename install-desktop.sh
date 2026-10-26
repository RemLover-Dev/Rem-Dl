#!/bin/sh
# Rems Dl -- Linux Desktop Integration Helper
# Installs application shortcut and icon into ~/.local/share/applications and ~/.local/share/icons

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_PATH="$SCRIPT_DIR/Rems_Dl"
ICON_SOURCE="$SCRIPT_DIR/icon/icon.png"

if [ ! -f "$BIN_PATH" ]; then
    # In source checkouts, Rems_Dl may be invoked via Python
    if [ -f "$SCRIPT_DIR/Rems_Dl.py" ]; then
        BIN_PATH="python3 $SCRIPT_DIR/Rems_Dl.py"
    else
        echo "Error: Rems_Dl executable not found in $SCRIPT_DIR" >&2
        exit 1
    fi
fi

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

mkdir -p "$APPS_DIR"
mkdir -p "$ICONS_DIR"

if [ -f "$ICON_SOURCE" ]; then
    cp "$ICON_SOURCE" "$ICONS_DIR/rems-dl.png"
    INSTALLED_ICON="$ICONS_DIR/rems-dl.png"
else
    INSTALLED_ICON="rems-dl"
fi

DESKTOP_FILE="$APPS_DIR/Rems_Dl.desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Rems Dl
Comment=Multi-threaded image & media scraper with glass-morphism web UI
Exec=$BIN_PATH
Path=$SCRIPT_DIR
Icon=$INSTALLED_ICON
Terminal=false
Categories=Graphics;Network;
StartupWMClass=Rems Dl
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

echo "Successfully installed Rems Dl desktop launcher to $DESKTOP_FILE"
echo "You can now launch Rems Dl from your system application menu."
