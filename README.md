# unsplash-wallpaper

Lädt einmal täglich ein zufälliges Foto von Unsplash herunter und setzt es als
Hintergrundbild unter Ubuntu/GNOME. Reines Python 3 aus der Standardbibliothek,
keine Abhängigkeiten zu installieren.

## Installation

```bash
./install.sh
```

Andere Uhrzeit für den täglichen Lauf:

```bash
RUN_AT=07:30 ./install.sh
```

Der Installer legt `~/.local/bin/unsplash-wallpaper` an und aktiviert einen
systemd-User-Timer. `Persistent=true` sorgt dafür, dass der Lauf nachgeholt
wird, wenn der Rechner zur eingestellten Zeit ausgeschaltet war.

## Unsplash Access Key (optional, empfohlen)

Ohne Key nutzt das Programm **Lorem Picsum** als Quelle — das liefert ebenfalls
Unsplash-Fotos, aber ohne Suchbegriffe und in geringerer Qualität.

Mit Key bekommst du gezielte Suchbegriffe, Collections und Bilder in voller
Auflösung:

1. https://unsplash.com/oauth/applications → *New Application*
   (Demo-Zugang, kostenlos, 50 Anfragen/Stunde — reicht für einmal täglich)
2. **Access Key** kopieren
3. In `~/.config/unsplash-wallpaper/config.ini` eintragen:

```ini
[unsplash]
access_key = dein_access_key
```

## Grafische Oberfläche

```bash
unsplash-wallpaper-gui
```

Liegt nach der Installation auch im App-Menü als **Unsplash Wallpaper**.
GTK4/libadwaita, drei Seiten:

- **Aktuell** — Vorschau des laufenden Hintergrundbilds mit Fotograf, Suchfeld
  für einen einmaligen Begriff und Knopf für ein neues Bild.
- **Verlauf** — Raster aller heruntergeladenen Bilder mit Datum darunter.
  Ein Klick setzt eines davon wieder als Hintergrund.
- **Einstellungen** — Access Key, Suchbegriffe, Collections, Ausrichtung,
  Skalierung, Anzahl behaltener Bilder, Benachrichtigung und Zeitplan
  (an/aus plus Uhrzeit). Änderungen landen direkt in der `config.ini`,
  Kommentare bleiben dabei erhalten.

Alle Zeitangaben stehen ausgeschrieben da — »Gesetzt vor 3 Minuten«,
»Nächster Lauf heute um 09:03 · zuletzt vor 23 Minuten«, im Verlauf
»Heute, 00:01« bzw. »17. August«. Die Monats- und Wochentagsnamen sind fest
auf Deutsch hinterlegt, damit die Anzeige nicht davon abhängt, mit welchem
`LANG` die Anwendung gestartet wird.

Die GUI ruft dieselbe Logik auf wie die Kommandozeile — es gibt keine zweite
Implementierung. Fehlt PyGObject, überspringt der Installer die GUI; nachrüsten
mit `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`.

## Logo

`icons/` enthält zwei handgeschriebene SVGs:

| Datei | Zweck |
|---|---|
| `de.zurek.UnsplashWallpaper.svg` | App-Symbol, 128×128, farbig |
| `de.zurek.UnsplashWallpaper-symbolic.svg` | Symbolische Fassung, einfarbig, 16×16 |

Motiv ist eine Berglandschaft mit Sonne und See in drei Tiefenebenen: dunstige
Kette hinten, zwei dunklere davor. Der Installer legt sie unter
`~/.local/share/icons/hicolor/` ab, wodurch Desktop-Eintrag, Fenster und
Benachrichtigungen sie über den Namen `de.zurek.UnsplashWallpaper` finden.

Die symbolische Fassung ist bewusst auf Sonne und zwei Gipfel reduziert, weil
im farbigen Symbol bei 16 px kein Detail mehr erkennbar bleibt.

## Benutzung

```bash
unsplash-wallpaper                      # Jetzt ein neues Wallpaper setzen
unsplash-wallpaper --query "snow alps"  # Einmalig mit anderem Suchbegriff
unsplash-wallpaper --status             # Aktuelles Bild, Fotograf, Timer
unsplash-wallpaper -v                   # Mit Debug-Ausgaben
```

## Konfiguration

`~/.config/unsplash-wallpaper/config.ini`

| Schlüssel | Bedeutung |
|---|---|
| `access_key` | Unsplash Access Key. Leer → Lorem Picsum |
| `query` | Suchbegriffe, kommagetrennt. Pro Lauf wird einer zufällig gewählt |
| `collections` | Unsplash-Collection-IDs. Haben Vorrang vor `query` |
| `orientation` | `landscape`, `portrait` oder `squarish` |
| `content_filter` | `high` (jugendfrei) oder `low` |
| `directory` | Zielordner, Standard `~/Pictures/Wallpapers` |
| `keep` | Anzahl der behaltenen Bilder, ältere werden gelöscht. `0` = alle behalten |
| `picture_options` | GNOME-Skalierung: `zoom`, `scaled`, `centered`, `stretched`, `spanned` |
| `width` / `height` | `auto` erkennt die Monitorauflösung über GNOME/Mutter |
| `quality` | JPEG-Qualität 1–100 |
| `notify` | Desktop-Benachrichtigung mit Fotografennamen |

Änderungen wirken sofort, kein Neustart des Timers nötig.

## Timer verwalten

```bash
systemctl --user list-timers unsplash-wallpaper.timer   # Nächster Lauf
systemctl --user start unsplash-wallpaper.service       # Jetzt ausführen
systemctl --user stop unsplash-wallpaper.timer          # Pausieren
systemctl --user start unsplash-wallpaper.timer         # Fortsetzen
journalctl --user -u unsplash-wallpaper.service -n 50   # Protokoll
```

Uhrzeit nachträglich ändern: in der GUI unter *Einstellungen → Zeitplan*, oder
`RUN_AT=18:00 ./install.sh`. Ein `./install.sh` ohne `RUN_AT` behält eine bereits
eingestellte Uhrzeit bei.

## Wie es funktioniert

- **Quelle** — `GET /photos/random` der Unsplash-API mit `query`, `orientation`
  und `content_filter`. Die `raw`-URL wird um imgix-Parameter für die erkannte
  Monitorauflösung ergänzt. Der von den API-Richtlinien verlangte
  `download_location`-Ping wird ausgelöst.
- **Download** — geht in eine `.part`-Datei und wird erst nach Prüfung der
  Magic Bytes und der Dateigröße umbenannt. Ein abgebrochener Download setzt
  also nie ein kaputtes Hintergrundbild.
- **Netzwerk** — bis zu 5 Versuche mit wachsender Wartezeit (~5 Minuten
  insgesamt). Das deckt den Fall ab, dass der Timer direkt nach dem Login
  feuert, während WLAN noch nicht verbunden ist. Bei HTTP 401/403 wird sofort
  abgebrochen und auf Lorem Picsum ausgewichen.
- **Setzen** — `gsettings` auf `picture-uri`, `picture-uri-dark` (Dark Mode) und
  den Sperrbildschirm. Fehlt `DBUS_SESSION_BUS_ADDRESS`, wird sie aus
  `/run/user/$UID/bus` abgeleitet, damit es auch aus der systemd-Unit
  funktioniert.

## Dateien

```
~/.local/bin/unsplash-wallpaper                    Programm (Kommandozeile)
~/.local/bin/unsplash-wallpaper-gui                Grafische Oberfläche
~/.config/unsplash-wallpaper/config.ini            Konfiguration
~/.config/systemd/user/unsplash-wallpaper.{service,timer}
~/.local/state/unsplash-wallpaper/wallpaper.log    Protokoll (rotiert)
~/.local/state/unsplash-wallpaper/current.json     Metadaten des aktuellen Bilds
~/.local/state/unsplash-wallpaper/history.json     Fotograf je heruntergeladenem Bild
~/.local/share/applications/de.zurek.UnsplashWallpaper.desktop
~/.local/share/icons/hicolor/scalable/apps/de.zurek.UnsplashWallpaper.svg
~/.local/share/icons/hicolor/symbolic/apps/de.zurek.UnsplashWallpaper-symbolic.svg
~/Pictures/Wallpapers/                             Bilder
```

## Deinstallation

```bash
./uninstall.sh
```

Entfernt Timer, Service und Programm. Bilder, Konfiguration und Logs bleiben
erhalten und können von Hand gelöscht werden.
