#!/usr/bin/env bash
# Removes Workspace View (after putting back any windows a viewer left parked).
set -euo pipefail
APP_DIR="$HOME/.local/share/desktopindesktop"
pkill -f "$APP_DIR/wsview.py" 2>/dev/null && sleep 1 || true
[ -f "$APP_DIR/wsview.py" ] && /usr/bin/python3 "$APP_DIR/wsview.py" --restore || true
rm -rf "$APP_DIR"
rm -f "$HOME/.local/bin/workspace-view" \
      "$HOME/.local/share/applications/workspace-view.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/workspace-view.svg"
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Workspace View uninstalled."
