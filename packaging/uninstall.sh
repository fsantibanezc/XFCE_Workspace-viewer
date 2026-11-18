#!/usr/bin/env bash
# Removes XFCE Workspace Viewer (after putting back any windows a viewer left parked).
set -euo pipefail
SLUG=xfce-workspace-viewer
APP_DIR="$HOME/.local/share/$SLUG"
pkill -f "^/usr/bin/python3 $APP_DIR/xfce_workspace_viewer.py" 2>/dev/null && sleep 1 || true
[ -f "$APP_DIR/xfce_workspace_viewer.py" ] && /usr/bin/python3 "$APP_DIR/xfce_workspace_viewer.py" --restore || true
rm -rf "$APP_DIR"
rm -f "$HOME/.local/bin/$SLUG" \
      "$HOME/.local/share/applications/$SLUG.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/$SLUG.svg"
# leftovers from the pre-rename "Workspace View" install
rm -f "$HOME/.local/bin/workspace-view" \
      "$HOME/.local/share/applications/workspace-view.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/workspace-view.svg"
rm -rf "$HOME/.local/share/desktopindesktop"
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "XFCE Workspace Viewer uninstalled."
