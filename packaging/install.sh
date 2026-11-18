#!/usr/bin/env bash
# Installs Workspace View for the current user and adds it to the XFCE (freedesktop) application menu.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$HOME/.local/share/desktopindesktop"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
PY=/usr/bin/python3

# --- dependency checks -------------------------------------------------------------
missing=()
"$PY" -c "import PyQt5.QtWidgets" 2>/dev/null || missing+=("python3-pyqt5")
command -v wmctrl >/dev/null || missing+=("wmctrl")
for lib in libXcomposite.so.1 libXdamage.so.1 libXext.so.6; do
  ldconfig -p | grep -q "$lib" || missing+=("$lib")
done
if ((${#missing[@]})); then
  echo "Missing dependencies: ${missing[*]}"
  echo "Install with:  sudo apt install python3-pyqt5 wmctrl libxcomposite1 libxdamage1 libxext6"
  exit 1
fi

# --- files -----------------------------------------------------------------------------
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
install -m 644 "$SRC"/x11util.py "$SRC"/viewer.py "$APP_DIR"/
install -m 755 "$SRC"/wsview.py "$SRC"/did.py "$APP_DIR"/
install -m 644 "$SRC"/packaging/workspace-view.svg "$ICON_DIR"/workspace-view.svg

cat > "$BIN_DIR/workspace-view" <<SH
#!/bin/sh
exec $PY "$APP_DIR/wsview.py" "\$@"
SH
chmod 755 "$BIN_DIR/workspace-view"

cat > "$DESKTOP_DIR/workspace-view.desktop" <<DESK
[Desktop Entry]
Type=Application
Version=1.0
Name=Workspace View
GenericName=Live workspace viewer
Comment=Watch the windows on another workspace live, in a window
Exec=$BIN_DIR/workspace-view
Icon=workspace-view
Terminal=false
Categories=Utility;
Keywords=workspace;desktop;virtual;live;view;monitor;windows;
StartupNotify=false
Actions=restore;

[Desktop Action restore]
Name=Restore parked windows
Exec=$BIN_DIR/workspace-view --restore
DESK

update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed Workspace View:"
echo "  menu:    Applications → Accessories → Workspace View"
echo "  command: workspace-view [--workspace N] [--fps 5] | workspace-view --restore"
echo "  files:   $APP_DIR"
