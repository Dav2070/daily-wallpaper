#!/usr/bin/env bash
# Entfernt Timer, Service und Skript. Bilder und Konfiguration bleiben erhalten.
set -euo pipefail

APP="unsplash-wallpaper"
UNIT_DIR="$HOME/.config/systemd/user"

systemctl --user disable --now "$APP.timer" 2>/dev/null || true
rm -f "$UNIT_DIR/$APP.timer" "$UNIT_DIR/$APP.service"
systemctl --user daemon-reload
rm -f "$HOME/.local/bin/$APP" "$HOME/.local/bin/$APP-gui"
rm -f "$HOME/.local/share/applications/de.zurek.UnsplashWallpaper.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/de.zurek.UnsplashWallpaper.svg"
rm -f "$HOME/.local/share/icons/hicolor/symbolic/apps/de.zurek.UnsplashWallpaper-symbolic.svg"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "Entfernt. Behalten wurden:"
echo "  Konfiguration : $HOME/.config/$APP/"
echo "  Bilder        : siehe 'directory' in der Konfiguration"
echo "  Logs          : $HOME/.local/state/$APP/"
