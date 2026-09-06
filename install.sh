#!/usr/bin/env bash
# Installiert unsplash-wallpaper als täglichen systemd-User-Timer.
set -euo pipefail

APP="unsplash-wallpaper"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"

# Uhrzeit des täglichen Laufs, überschreibbar:  RUN_AT=07:30 ./install.sh
# Ohne RUN_AT bleibt eine bereits eingestellte Zeit erhalten (z. B. aus der GUI).
if [ -z "${RUN_AT:-}" ] && [ -f "$HOME/.config/systemd/user/$APP.timer" ]; then
  RUN_AT="$(sed -n 's/^OnCalendar=.*[[:space:]]\([0-9][0-9]:[0-9][0-9]\).*$/\1/p' \
            "$HOME/.config/systemd/user/$APP.timer" | head -1)"
fi
RUN_AT="${RUN_AT:-09:00}"

command -v gsettings >/dev/null || { echo "Fehler: gsettings nicht gefunden (GNOME nötig)."; exit 1; }
command -v python3   >/dev/null || { echo "Fehler: python3 nicht gefunden."; exit 1; }

echo "==> Skripte nach $BIN_DIR installieren"
mkdir -p "$BIN_DIR"
install -m 755 "$SRC_DIR/unsplash-wallpaper.py" "$BIN_DIR/$APP"

# Grafische Oberfläche -- optional, braucht PyGObject mit GTK4 und libadwaita
GUI_OK=no
if python3 -c "
import gi
gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')
from gi.repository import Gtk, Adw
" 2>/dev/null; then
  install -m 755 "$SRC_DIR/unsplash-wallpaper-gui.py" "$BIN_DIR/$APP-gui"
  GUI_OK=yes

  echo "==> Symbole installieren"
  ICON_DIR="$HOME/.local/share/icons/hicolor"
  install -Dm644 "$SRC_DIR/icons/de.zurek.UnsplashWallpaper.svg" \
                 "$ICON_DIR/scalable/apps/de.zurek.UnsplashWallpaper.svg"
  install -Dm644 "$SRC_DIR/icons/de.zurek.UnsplashWallpaper-symbolic.svg" \
                 "$ICON_DIR/symbolic/apps/de.zurek.UnsplashWallpaper-symbolic.svg"
  gtk4-update-icon-cache -qtf "$ICON_DIR" 2>/dev/null ||
    gtk-update-icon-cache -qtf "$ICON_DIR" 2>/dev/null || true

  echo "==> Desktop-Eintrag anlegen"
  DESKTOP_DIR="$HOME/.local/share/applications"
  mkdir -p "$DESKTOP_DIR"
  cat > "$DESKTOP_DIR/de.zurek.UnsplashWallpaper.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Unsplash Wallpaper
Comment=Täglich ein neues Hintergrundbild von Unsplash
Exec=$BIN_DIR/$APP-gui
Icon=de.zurek.UnsplashWallpaper
Terminal=false
Categories=Utility;GTK;
Keywords=wallpaper;hintergrund;unsplash;desktop;
StartupNotify=true
StartupWMClass=de.zurek.UnsplashWallpaper
DESKTOP
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
else
  echo "==> GTK4/libadwaita fehlt -- GUI wird übersprungen"
  echo "    Nachinstallieren mit: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
fi

echo "==> Übersetzungen kompilieren"
python3 "$SRC_DIR/tools/i18n.py" compile \
  --po-dir "$SRC_DIR/po" --output "$HOME/.local/share/locale"

echo "==> systemd-Units nach $UNIT_DIR schreiben"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/$APP.service" <<UNIT
[Unit]
Description=Tägliches Hintergrundbild von Unsplash
Documentation=file://$SRC_DIR/README.md
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/$APP
# Nur zur Sicherheit -- normalerweise erbt der User-Manager diese Variablen
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
TimeoutStartSec=15min
UNIT

cat > "$UNIT_DIR/$APP.timer" <<UNIT
[Unit]
Description=Startet $APP einmal täglich

[Timer]
OnCalendar=*-*-* $RUN_AT
# Läuft nach, falls der Rechner zum Zeitpunkt aus war
Persistent=true
# Verteilt die Last, falls mehrere Rechner denselben Timer nutzen
RandomizedDelaySec=20min
Unit=$APP.service

[Install]
WantedBy=timers.target
UNIT

echo "==> Timer aktivieren"
systemctl --user daemon-reload
systemctl --user enable --now "$APP.timer"

# PATH-Hinweis, falls ~/.local/bin nicht eingebunden ist
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Hinweis: $BIN_DIR liegt nicht im PATH. Ergänze in ~/.bashrc:"
     echo "         export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

CONFIG="$HOME/.config/$APP/config.ini"
"$BIN_DIR/$APP" --config >/dev/null 2>&1 || true

cat <<MSG

Fertig.

  Konfiguration : $CONFIG
  Nächster Lauf : täglich um $RUN_AT (mit bis zu 20 min Verzögerung)

Optional, aber empfohlen: kostenlosen Unsplash Access Key eintragen
  1. https://unsplash.com/oauth/applications  ->  "New Application"
  2. "Access Key" kopieren
  3. In $CONFIG bei access_key = einfügen
Ohne Key läuft alles über Lorem Picsum (liefert ebenfalls Unsplash-Fotos).

Sofort ausprobieren:   $APP
Status ansehen:        $APP --status
MSG

if [ "$GUI_OK" = yes ]; then
  echo "Grafische Oberfläche:  $APP-gui   (auch im App-Menü als \"Unsplash Wallpaper\")"
  echo
fi
