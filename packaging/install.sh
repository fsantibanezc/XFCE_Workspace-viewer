#!/usr/bin/env bash
# Installs XFCE Workspace Viewer for the current user and adds it to the XFCE (freedesktop) application menu.
set -euo pipefail

SLUG=xfce-workspace-viewer
NAME="XFCE Workspace Viewer"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$HOME/.local/share/$SLUG"
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

# --- remove a pre-rename install ("Workspace View"), if present ------------------------
OLD_APP_DIR="$HOME/.local/share/desktopindesktop"
if [ -f "$OLD_APP_DIR/wsview.py" ]; then
  "$PY" "$OLD_APP_DIR/wsview.py" --restore >/dev/null 2>&1 || true
fi
rm -f "$BIN_DIR/workspace-view" "$DESKTOP_DIR/workspace-view.desktop" "$ICON_DIR/workspace-view.svg"
rm -f "$OLD_APP_DIR"/{wsview.py,x11util.py,viewer.py,did.py}
rm -rf "$OLD_APP_DIR/__pycache__"
rmdir "$OLD_APP_DIR" 2>/dev/null || true

# --- files -----------------------------------------------------------------------------
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
rm -rf "$APP_DIR/__pycache__"
install -m 644 "$SRC"/x11util.py "$SRC"/viewer.py "$APP_DIR"/
install -m 755 "$SRC"/xfce_workspace_viewer.py "$SRC"/did.py "$APP_DIR"/
install -m 644 "$SRC/packaging/$SLUG.svg" "$ICON_DIR/$SLUG.svg"

cat > "$BIN_DIR/$SLUG" <<SH
#!/bin/sh
exec $PY "$APP_DIR/xfce_workspace_viewer.py" "\$@"
SH
chmod 755 "$BIN_DIR/$SLUG"

cat > "$DESKTOP_DIR/$SLUG.desktop" <<DESK
[Desktop Entry]
Type=Application
Version=1.0
Name=$NAME
GenericName=Live workspace viewer
Comment=Watch the windows on another workspace live, in a window
Exec=$BIN_DIR/$SLUG
Icon=$SLUG
Terminal=false
Categories=Utility;
Keywords=workspace;desktop;virtual;live;view;monitor;windows;xfce;
StartupNotify=false
StartupWMClass=xfce_workspace_viewer.py
Actions=restore;

[Desktop Action restore]
Name=Restore parked windows
Exec=$BIN_DIR/$SLUG --restore
DESK

update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed $NAME:"
echo "  menu:    Applications → Accessories → $NAME"
echo "  command: $SLUG [--workspace N] [--fps 5] | $SLUG --restore"
echo "  files:   $APP_DIR"
