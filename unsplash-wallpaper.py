#!/usr/bin/env python3
"""Lädt ein zufälliges Bild von Unsplash und setzt es als GNOME-Hintergrundbild.

Nur Python-Standardbibliothek, keine externen Abhängigkeiten.

Aufrufe:
    unsplash-wallpaper.py            Neues Wallpaper holen und setzen
    unsplash-wallpaper.py --status   Zeigt aktuelles Wallpaper + Konfiguration
    unsplash-wallpaper.py --query "misty forest"   Suchbegriff für diesen Lauf
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

APP = "unsplash-wallpaper"
USER_AGENT = f"{APP}/1.0 (+https://unsplash.com/developers)"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
CONFIG_FILE = CONFIG_DIR / "config.ini"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / APP
LOG_FILE = STATE_DIR / "wallpaper.log"
CURRENT_JSON = STATE_DIR / "current.json"
HISTORY_JSON = STATE_DIR / "history.json"

DEFAULT_CONFIG = """\
[unsplash]
# Kostenloser Access Key von https://unsplash.com/oauth/applications
# Ohne Key wird automatisch Lorem Picsum (liefert ebenfalls Unsplash-Fotos)
# als Ersatzquelle benutzt.
access_key =

# Suchbegriffe, kommagetrennt. Pro Lauf wird einer zufällig gewählt.
# Leer lassen für komplett zufällige Fotos.
query = landscape, nature, mountains, ocean

# Optional: IDs von Unsplash-Collections, kommagetrennt (statt query).
collections =

# landscape | portrait | squarish
orientation = landscape

# Nur Fotos, die als "Content-Safe" markiert sind
content_filter = high

[wallpaper]
# Zielordner für die heruntergeladenen Bilder
directory = ~/Pictures/Wallpapers

# So viele Bilder behalten, ältere werden gelöscht (0 = alle behalten)
keep = 10

# zoom | scaled | centered | stretched | wallpaper | spanned
picture_options = zoom

# Bildbreite/-höhe. "auto" ermittelt die Auflösung über GNOME/Mutter.
width = auto
height = auto

# JPEG-Qualität beim Ausliefern durch Unsplash (1-100)
quality = 85

# Desktop-Benachrichtigung mit Fotograf anzeigen
notify = true
"""

log = logging.getLogger(APP)


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

def load_config() -> configparser.ConfigParser:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG)
        log.info("Standardkonfiguration angelegt: %s", CONFIG_FILE)

    cfg = configparser.ConfigParser()
    cfg.read_string(DEFAULT_CONFIG)      # Defaults als Basis ...
    cfg.read(CONFIG_FILE)                # ... vom User überschrieben
    return cfg


def unquote(value: str) -> str:
    """Entfernt umschließende Anführungszeichen.

    configparser behandelt sie als Teil des Werts -- ein in Quotes eingefügter
    Access Key würde sonst einen 401 auslösen.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def csv_list(value: str) -> list[str]:
    return [unquote(item) for item in value.split(",") if unquote(item)]


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=256 * 1024, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    log.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(stream_handler)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout)


def http_json(url: str, headers: dict[str, str] | None = None) -> dict:
    with http_get(url, headers) as response:
        return json.load(response)


def with_retries(func, attempts: int = 5, base_delay: float = 5.0):
    """Ruft func() auf und wiederholt bei Netzwerkfehlern mit wachsender Pause.

    Deckt den Fall ab, dass der Timer direkt nach dem Login feuert und die
    Netzwerkverbindung noch nicht steht.
    """
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as err:
            if isinstance(err, urllib.error.HTTPError) and err.code in (401, 403):
                raise  # Falscher/fehlender Key -- erneuter Versuch bringt nichts
            if attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 3)
            log.warning("Versuch %d/%d fehlgeschlagen (%s), warte %.0fs",
                        attempt, attempts, err, delay)
            time.sleep(delay)
    raise RuntimeError("unerreichbar")


# --------------------------------------------------------------------------- #
# Bildquellen
# --------------------------------------------------------------------------- #

@dataclass
class Photo:
    source: str
    id: str
    image_url: str
    author: str = "Unbekannt"
    author_url: str = ""
    page_url: str = ""
    description: str = ""
    download_location: str = ""   # Unsplash-Tracking-Endpoint


def sized_url(raw_url: str, width: int, height: int, quality: int) -> str:
    """Hängt die imgix-Parameter an eine Unsplash-raw-URL an."""
    parts = urllib.parse.urlsplit(raw_url)
    params = dict(urllib.parse.parse_qsl(parts.query))
    params.update({
        "w": str(width),
        "h": str(height),
        "fit": "crop",
        "crop": "entropy",
        "fm": "jpg",
        "q": str(quality),
    })
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params)))


def fetch_from_unsplash(cfg, access_key: str, query: str | None,
                        width: int, height: int) -> Photo:
    params = {"orientation": cfg["unsplash"]["orientation"],
              "content_filter": cfg["unsplash"]["content_filter"]}

    collections = csv_list(cfg["unsplash"]["collections"])
    if collections:
        params["collections"] = ",".join(collections)
    elif query:
        params["query"] = query

    url = "https://api.unsplash.com/photos/random?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Client-ID {access_key}",
               "Accept-Version": "v1"}

    log.info("Frage Unsplash-API an (query=%r, orientation=%s)",
             query or "-", params["orientation"])
    data = with_retries(lambda: http_json(url, headers))

    if isinstance(data, list):        # kommt vor, wenn count gesetzt wäre
        data = data[0]

    user = data.get("user") or {}
    quality = cfg["wallpaper"].getint("quality")
    return Photo(
        source="unsplash",
        id=data.get("id", "unknown"),
        image_url=sized_url(data["urls"]["raw"], width, height, quality),
        author=user.get("name") or "Unbekannt",
        author_url=(user.get("links") or {}).get("html", ""),
        page_url=(data.get("links") or {}).get("html", ""),
        description=(data.get("description") or data.get("alt_description") or "").strip(),
        download_location=(data.get("links") or {}).get("download_location", ""),
    )


def fetch_from_picsum(width: int, height: int) -> Photo:
    """Ersatzquelle ohne API-Key. Lorem Picsum liefert Fotos von Unsplash aus."""
    log.info("Kein Unsplash-Key konfiguriert -- benutze Lorem Picsum")
    url = f"https://picsum.photos/{width}/{height}"

    def resolve() -> tuple[str, str]:
        with http_get(url) as response:
            return response.geturl(), response.headers.get("picsum-id", "")

    final_url, photo_id = with_retries(resolve)
    if not photo_id:
        match = re.search(r"/id/(\d+)/", final_url)
        photo_id = match.group(1) if match else "unknown"

    photo = Photo(source="picsum", id=photo_id, image_url=final_url)
    try:
        info = http_json(f"https://picsum.photos/id/{photo_id}/info")
        photo.author = info.get("author") or photo.author
        photo.page_url = info.get("url", "")
    except Exception as err:                       # nur Metadaten, nicht kritisch
        log.debug("Picsum-Metadaten nicht abrufbar: %s", err)
    return photo


def notify_unsplash_download(photo: Photo, access_key: str) -> None:
    """Von den Unsplash-API-Richtlinien verlangter Download-Ping."""
    if not (photo.download_location and access_key):
        return
    try:
        http_json(photo.download_location, {"Authorization": f"Client-ID {access_key}"})
        log.debug("Unsplash-Download registriert")
    except Exception as err:
        log.debug("Download-Ping fehlgeschlagen: %s", err)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")


def download_image(photo: Photo, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%Y-%m-%d')}_{photo.source}_{photo.id}"
    final_path = target_dir / f"{stem}.jpg"
    temp_path = target_dir / f".{stem}.part"

    def download() -> None:
        with http_get(photo.image_url, timeout=120) as response, temp_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)

    with_retries(download)

    header = temp_path.read_bytes()[:8]
    if not any(header.startswith(magic) for magic in IMAGE_MAGIC):
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("Heruntergeladene Datei ist kein Bild")
    if temp_path.stat().st_size < 10_000:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("Heruntergeladene Datei ist verdächtig klein")

    temp_path.replace(final_path)
    log.info("Gespeichert: %s (%.1f MB)", final_path, final_path.stat().st_size / 1e6)
    return final_path


def load_history() -> dict:
    """Metadaten je Bilddatei: {dateiname: {author, page_url, ...}}."""
    try:
        return json.loads(HISTORY_JSON.read_text())
    except (OSError, ValueError):
        return {}


def remember_photo(path: Path, photo: Photo) -> None:
    """Merkt sich Fotograf und Quelle, damit ein Bild aus dem Verlauf später
    noch korrekt zugeordnet werden kann."""
    history = load_history()
    history[path.name] = asdict(photo)
    history = {name: meta for name, meta in history.items()
               if (path.parent / name).exists()}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_JSON.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def prune_old(target_dir: Path, keep: int, protect: Path) -> None:
    if keep <= 0:
        return
    images = sorted(
        (p for p in target_dir.glob("*.jpg") if p != protect),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in images[max(keep - 1, 0):]:
        old.unlink(missing_ok=True)
        log.info("Gelöscht (Aufräumen): %s", old.name)


# --------------------------------------------------------------------------- #
# GNOME
# --------------------------------------------------------------------------- #

def ensure_session_bus() -> None:
    """Sorgt dafür, dass gsettings/dconf den Session-Bus finden.

    Nötig, wenn das Skript aus einer systemd-Unit ohne geerbte Umgebung läuft.
    """
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    bus = Path(f"/run/user/{os.getuid()}/bus")
    if bus.exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        log.debug("DBUS_SESSION_BUS_ADDRESS gesetzt auf %s", bus)


def gsettings(*args: str) -> str:
    result = subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError(f"gsettings {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def detect_resolution() -> tuple[int, int]:
    """Größte aktuell aktive Monitorauflösung über die Mutter-DBus-Schnittstelle."""
    fallback = (2560, 1440)
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.gnome.Mutter.DisplayConfig",
             "--object-path", "/org/gnome/Mutter/DisplayConfig",
             "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return fallback
        modes = re.findall(r"'(\d+)x(\d+)@[\d.]+'[^)]*?'is-current': <true>", result.stdout)
        if not modes:
            return fallback
        width, height = max(((int(w), int(h)) for w, h in modes), key=lambda wh: wh[0] * wh[1])
        log.debug("Auflösung erkannt: %dx%d", width, height)
        return width, height
    except Exception as err:
        log.debug("Auflösungserkennung fehlgeschlagen: %s", err)
        return fallback


def set_wallpaper(path: Path, picture_options: str) -> None:
    uri = path.resolve().as_uri()
    gsettings("set", "org.gnome.desktop.background", "picture-uri", uri)
    gsettings("set", "org.gnome.desktop.background", "picture-uri-dark", uri)
    gsettings("set", "org.gnome.desktop.background", "picture-options", picture_options)
    try:
        gsettings("set", "org.gnome.desktop.screensaver", "picture-uri", uri)
    except RuntimeError as err:
        log.debug("Sperrbildschirm nicht gesetzt: %s", err)
    log.info("Hintergrundbild gesetzt: %s", path.name)


def send_notification(photo: Photo, path: Path) -> None:
    if not shutil.which("notify-send"):
        return
    credit = f"Foto von {photo.author}"
    if photo.description:
        credit += f"\n{photo.description[:120]}"
    try:
        subprocess.run(
            ["notify-send", "--app-name=Wallpaper", f"--icon={path}",
             "--hint=string:desktop-entry:de.zurek.UnsplashWallpaper",
             "Neues Hintergrundbild", credit],
            timeout=10, check=False,
        )
    except Exception as err:
        log.debug("Benachrichtigung fehlgeschlagen: %s", err)


# --------------------------------------------------------------------------- #
# Abläufe
# --------------------------------------------------------------------------- #

def run_update(cfg: configparser.ConfigParser, query_override: str | None) -> int:
    ensure_session_bus()

    wallpaper_cfg = cfg["wallpaper"]
    target_dir = Path(wallpaper_cfg["directory"]).expanduser()

    if wallpaper_cfg["width"].strip().lower() == "auto" or \
       wallpaper_cfg["height"].strip().lower() == "auto":
        width, height = detect_resolution()
    else:
        width, height = wallpaper_cfg.getint("width"), wallpaper_cfg.getint("height")

    access_key = unquote(cfg["unsplash"]["access_key"])
    queries = csv_list(cfg["unsplash"]["query"])
    query = query_override or (random.choice(queries) if queries else None)

    if access_key:
        try:
            photo = fetch_from_unsplash(cfg, access_key, query, width, height)
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                log.error("Unsplash lehnt den Access Key ab (HTTP %d) -- "
                          "weiter mit Lorem Picsum", err.code)
                photo = fetch_from_picsum(width, height)
            else:
                raise
    else:
        photo = fetch_from_picsum(width, height)

    path = download_image(photo, target_dir)
    notify_unsplash_download(photo, access_key)
    set_wallpaper(path, wallpaper_cfg["picture_options"])
    prune_old(target_dir, wallpaper_cfg.getint("keep"), protect=path)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    remember_photo(path, photo)
    CURRENT_JSON.write_text(json.dumps(
        {**asdict(photo), "path": str(path), "set_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        indent=2, ensure_ascii=False,
    ))

    if wallpaper_cfg.getboolean("notify"):
        send_notification(photo, path)

    log.info("Fertig -- Foto von %s (%s)", photo.author, photo.page_url or photo.source)
    return 0


def show_status(cfg: configparser.ConfigParser) -> int:
    ensure_session_bus()
    print(f"Konfiguration : {CONFIG_FILE}")
    print(f"Logdatei      : {LOG_FILE}")
    print(f"Bilderordner  : {Path(cfg['wallpaper']['directory']).expanduser()}")
    key = unquote(cfg["unsplash"]["access_key"])
    print(f"Unsplash-Key  : {'gesetzt (' + key[:6] + '...)' if key else 'nicht gesetzt -> Lorem Picsum'}")

    try:
        print(f"Aktuell aktiv : {gsettings('get', 'org.gnome.desktop.background', 'picture-uri')}")
    except RuntimeError as err:
        print(f"Aktuell aktiv : nicht lesbar ({err})")

    if CURRENT_JSON.exists():
        current = json.loads(CURRENT_JSON.read_text())
        print(f"Zuletzt       : {current.get('set_at')} -- Foto von {current.get('author')}")
        if current.get("page_url"):
            print(f"Quelle        : {current['page_url']}")

    result = subprocess.run(
        ["systemctl", "--user", "list-timers", f"{APP}.timer", "--no-pager"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        print("\nTimer:")
        print(result.stdout.rstrip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog=APP,
        description="Lädt täglich ein Bild von Unsplash und setzt es als GNOME-Hintergrund.",
    )
    parser.add_argument("--status", action="store_true",
                        help="Aktuellen Zustand und Timer anzeigen")
    parser.add_argument("--query", metavar="BEGRIFF",
                        help="Suchbegriff nur für diesen Lauf")
    parser.add_argument("--config", action="store_true",
                        help="Pfad der Konfigurationsdatei ausgeben")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-Ausgaben")
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config()

    if args.config:
        print(CONFIG_FILE)
        return 0
    if args.status:
        return show_status(cfg)

    try:
        return run_update(cfg, args.query)
    except Exception as err:
        log.error("Abbruch: %s", err)
        log.debug("Details:", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
