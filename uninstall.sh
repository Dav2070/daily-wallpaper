#!/usr/bin/env bash
# Entfernt Timer, Service und Skript. Bilder und Konfiguration bleiben erhalten.
set -euo pipefail

APP="daily-wallpaper"
# Vor der Umbenennung hiess das Projekt unsplash-wallpaper: Reste davon werden
# mit entfernt, die Ordner für Konfiguration und Status tragen den Namen weiter.
OLD_APP="unsplash-wallpaper"
DATA_NAME="unsplash-wallpaper"
UNIT_DIR="$HOME/.config/systemd/user"

for name in "$APP" "$OLD_APP"; do
  systemctl --user disable --now "$name.timer" 2>/dev/null || true
  rm -f "$UNIT_DIR/$name.timer" "$UNIT_DIR/$name.service"
  rm -f "$HOME/.local/bin/$name" "$HOME/.local/bin/$name-gui"
done
systemctl --user daemon-reload
rm -f "$HOME"/.local/share/locale/*/LC_MESSAGES/daily-wallpaper.mo
rm -f "$HOME/.local/share/applications/de.zurek.UnsplashWallpaper.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/de.zurek.UnsplashWallpaper.svg"
rm -f "$HOME/.local/share/icons/hicolor/symbolic/apps/de.zurek.UnsplashWallpaper-symbolic.svg"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "Entfernt. Behalten wurden:"
echo "  Konfiguration : $HOME/.config/$DATA_NAME/"
echo "  Bilder        : siehe 'directory' in der Konfiguration"
echo "  Logs          : $HOME/.local/state/$DATA_NAME/"
