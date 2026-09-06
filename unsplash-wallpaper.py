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
import gettext
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
from datetime import date, datetime
from pathlib import Path

APP = "unsplash-wallpaper"
USER_AGENT = f"{APP}/1.0 (+https://unsplash.com/developers)"

GETTEXT_DOMAIN = "daily-wallpaper"


def _find_localedir() -> str | None:
    """Sucht den Ordner, der die kompilierten Übersetzungen enthält."""
    candidates = []
    if snap := os.environ.get("SNAP"):
        candidates.append(Path(snap) / "usr/share/locale")
    candidates += [
        Path(__file__).resolve().parent / "locale",   # Projektordner
        Path.home() / ".local/share/locale",
        Path("/usr/share/locale"),
    ]
    for directory in candidates:
        if directory.is_dir() and any(
                directory.glob(f"*/LC_MESSAGES/{GETTEXT_DOMAIN}.mo")):
            return str(directory)
    return None


# Quellsprache ist Englisch: wer eine Locale ohne eigene Übersetzung hat,
# bekommt damit Englisch statt Deutsch zu sehen.
_translation = gettext.translation(GETTEXT_DOMAIN, _find_localedir(), fallback=True)
_ = _translation.gettext
ngettext = _translation.ngettext


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
CONFIG_FILE = CONFIG_DIR / "config.ini"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / APP
LOG_FILE = STATE_DIR / "wallpaper.log"
CURRENT_JSON = STATE_DIR / "current.json"
HISTORY_JSON = STATE_DIR / "history.json"

DEFAULT_CONFIG = """\
[unsplash]
# Free access key from https://unsplash.com/oauth/applications
# Without a key, Lorem Picsum (which also serves Unsplash photos) is used
# as a fallback source.
access_key =

# Search terms, comma separated. One is picked at random per run.
# Leave empty for completely random photos.
query = landscape, nature, mountains, ocean

# Optional: Unsplash collection IDs, comma separated (instead of query).
collections =

# landscape | portrait | squarish
orientation = landscape

# Only photos flagged as content-safe
content_filter = high

[wallpaper]
# Target folder for downloaded images
directory = ~/Pictures/Wallpapers

# Keep this many images, older ones are deleted (0 = keep all)
keep = 10

# zoom | scaled | centered | stretched | wallpaper | spanned
picture_options = zoom

# Image width/height. "auto" detects the monitor resolution.
width = auto
height = auto

# JPEG quality requested from Unsplash (1-100)
quality = 85

# Show a desktop notification naming the photographer
notify = true

[schedule]
# Evaluated by watch mode (--watch) and by --if-due. Outside a snap the
# systemd timer runs the job instead.
enabled = true

# Time of the daily change
time = 09:00

# Interval between checks in watch mode, in minutes
check_every = 15
"""

log = logging.getLogger(APP)


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

def load_config() -> configparser.ConfigParser:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG)
        log.info(_("Created default configuration: %s"), CONFIG_FILE)

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


def expand_path(value: str) -> Path:
    """Expandiert »~«.

    In einem Snap zeigt HOME auf $SNAP_USER_DATA. Die Bilder müssen aber im
    echten Home liegen, sonst kann die GNOME Shell sie nicht lesen -- dafür
    setzt snapd SNAP_REAL_HOME.
    """
    text = unquote(value)
    real_home = os.environ.get("SNAP_REAL_HOME")
    if real_home and (text == "~" or text.startswith("~/")):
        return Path(real_home + text[1:])
    return Path(text).expanduser()


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
            log.warning(_("Attempt %d/%d failed (%s), retrying in %.0fs"),
                        attempt, attempts, err, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- #
# Bildquellen
# --------------------------------------------------------------------------- #

@dataclass
class Photo:
    source: str
    id: str
    image_url: str
    author: str = "Unknown"
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

    log.info(_("Querying the Unsplash API (query=%r, orientation=%s)"),
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
        author=user.get("name") or _("Unknown"),
        author_url=(user.get("links") or {}).get("html", ""),
        page_url=(data.get("links") or {}).get("html", ""),
        description=(data.get("description") or data.get("alt_description") or "").strip(),
        download_location=(data.get("links") or {}).get("download_location", ""),
    )


def fetch_from_picsum(width: int, height: int) -> Photo:
    """Ersatzquelle ohne API-Key. Lorem Picsum liefert Fotos von Unsplash aus."""
    log.info(_("No Unsplash key configured -- using Lorem Picsum"))
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
        log.debug("Picsum metadata unavailable: %s", err)
    return photo


def notify_unsplash_download(photo: Photo, access_key: str) -> None:
    """Von den Unsplash-API-Richtlinien verlangter Download-Ping."""
    if not (photo.download_location and access_key):
        return
    try:
        http_json(photo.download_location, {"Authorization": f"Client-ID {access_key}"})
        log.debug("Unsplash download registered")
    except Exception as err:
        log.debug("Download ping failed: %s", err)


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
        raise RuntimeError(_("The downloaded file is not an image"))
    if temp_path.stat().st_size < 10_000:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(_("The downloaded file is suspiciously small"))

    temp_path.replace(final_path)
    log.info(_("Saved: %s (%.1f MB)"), final_path, final_path.stat().st_size / 1e6)
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
        log.info(_("Deleted during cleanup: %s"), old.name)


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
        log.debug("DBUS_SESSION_BUS_ADDRESS set to %s", bus)


def gsettings(*args: str) -> str:
    result = subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError(f"gsettings {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def _resolution_from_gdk() -> tuple[int, int] | None:
    """Größte Monitorfläche über GDK.

    Bevorzugter Weg: im Snap sperrt AppArmor den DBus-Aufruf an Mutter, GDK
    funktioniert dagegen, weil die App Wayland- bzw. X11-Zugriff hat.
    """
    try:
        import gi
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk
    except (ImportError, ValueError) as err:
        log.debug("PyGObject unavailable: %s", err)
        return None

    try:
        if not Gtk.init_check():
            log.debug("No connection to the display server")
            return None
        display = Gdk.Display.get_default()
        if display is None:
            return None

        monitors = display.get_monitors()
        sizes = []
        for index in range(monitors.get_n_items()):
            monitor = monitors.get_item(index)
            area = monitor.get_geometry()
            scale = monitor.get_scale_factor() or 1
            sizes.append((area.width * scale, area.height * scale))
        if not sizes:
            return None
        return max(sizes, key=lambda size: size[0] * size[1])
    except Exception as err:
        log.debug("Could not determine resolution via GDK: %s", err)
        return None


def _resolution_from_mutter() -> tuple[int, int] | None:
    """Rückfall für Umgebungen ohne PyGObject."""
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.gnome.Mutter.DisplayConfig",
             "--object-path", "/org/gnome/Mutter/DisplayConfig",
             "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.debug("Mutter query denied: %s", result.stderr.strip()[:120])
            return None
        modes = re.findall(r"'(\d+)x(\d+)@[\d.]+'[^)]*?'is-current': <true>", result.stdout)
        if not modes:
            return None
        return max(((int(w), int(h)) for w, h in modes), key=lambda size: size[0] * size[1])
    except Exception as err:
        log.debug("Mutter query failed: %s", err)
        return None


def detect_resolution() -> tuple[int, int]:
    """Größte aktuell aktive Monitorauflösung."""
    fallback = (2560, 1440)
    for source, probe in (("GDK", _resolution_from_gdk),
                          ("Mutter", _resolution_from_mutter)):
        size = probe()
        if size:
            log.debug("Resolution detected via %s: %dx%d", source, *size)
            return size
    log.debug("Resolution unavailable, using %dx%d", *fallback)
    return fallback


def set_wallpaper(path: Path, picture_options: str) -> None:
    uri = path.resolve().as_uri()
    gsettings("set", "org.gnome.desktop.background", "picture-uri", uri)
    gsettings("set", "org.gnome.desktop.background", "picture-uri-dark", uri)
    gsettings("set", "org.gnome.desktop.background", "picture-options", picture_options)
    try:
        gsettings("set", "org.gnome.desktop.screensaver", "picture-uri", uri)
    except RuntimeError as err:
        log.debug("Lock screen not set: %s", err)
    log.info(_("Wallpaper set: %s"), path.name)


def _notify_via_gio(title: str, body: str, icon: Path, desktop_id: str) -> bool:
    """Benachrichtigung über Gio.

    Bevorzugt, weil ohne Introspection: »gdbus call« fragt vorher die
    Schnittstelle ab, und genau dieses Introspect verbietet AppArmor im Snap.
    """
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        return False

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        arguments = GLib.Variant(
            "(susssasa{sv}i)",
            ("Daily Wallpaper", 0, str(icon), title, body, [],
             {"desktop-entry": GLib.Variant("s", desktop_id)}, 5000),
        )
        bus.call_sync("org.freedesktop.Notifications",
                      "/org/freedesktop/Notifications",
                      "org.freedesktop.Notifications", "Notify",
                      arguments, GLib.VariantType("(u)"),
                      Gio.DBusCallFlags.NONE, 5000, None)
        return True
    except Exception as err:
        log.debug("Gio notification failed: %s", err)
        return False


def send_notification(photo: Photo, path: Path) -> None:
    title = _("New wallpaper")
    body = _("Photo by {author}").format(author=photo.author)
    if photo.description:
        body += f"\n{photo.description[:120]}"
    desktop_id = os.environ.get("DESKTOP_ENTRY", "de.zurek.UnsplashWallpaper")

    if _notify_via_gio(title, body, path, desktop_id):
        return

    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "--app-name=Wallpaper", f"--icon={path}",
                 f"--hint=string:desktop-entry:{desktop_id}", title, body],
                timeout=10, check=False, capture_output=True)
        except Exception as err:
            log.debug("Notification failed: %s", err)


# --------------------------------------------------------------------------- #
# Autostart (nur im Snap)
# --------------------------------------------------------------------------- #

AUTOSTART_FILE = "daily-wallpaper-watcher.desktop"


def autostart_path() -> Path | None:
    """Ziel der Autostart-Datei, oder None ausserhalb eines Snaps."""
    user_data = os.environ.get("SNAP_USER_DATA")
    if not user_data:
        return None
    return Path(user_data) / ".config/autostart" / AUTOSTART_FILE


def set_autostart(enabled: bool) -> bool:
    """Legt die Autostart-Datei an oder entfernt sie.

    snapd erzeugt sie nicht selbst: der Schlüssel »autostart« in snapcraft.yaml
    ordnet nur eine Datei einer App zu. Beim Anmelden startet
    »snap userd --autostart«, was in $SNAP_USER_DATA/.config/autostart liegt.
    """
    target = autostart_path()
    if target is None:
        return False

    try:
        if enabled:
            source = Path(os.environ["SNAP"]) / "meta/gui" / AUTOSTART_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text())
            log.debug("Autostart installed: %s", target)
        else:
            target.unlink(missing_ok=True)
            log.debug("Autostart removed")
        return True
    except OSError as err:
        log.warning(_("Could not change autostart: %s"), err)
        return False


def sync_autostart(cfg: configparser.ConfigParser) -> None:
    """Bringt die Autostart-Datei mit der Einstellung in Übereinstimmung."""
    target = autostart_path()
    if target is None:
        return
    wanted = cfg["schedule"].getboolean("enabled")
    if wanted != target.exists():
        set_autostart(wanted)


# --------------------------------------------------------------------------- #
# Zeitplan
# --------------------------------------------------------------------------- #

def scheduled_time(cfg: configparser.ConfigParser) -> tuple[int, int]:
    raw = unquote(cfg["schedule"]["time"])
    try:
        hour, minute = raw.split(":")
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except ValueError:
        log.warning(_("Invalid time %r in the schedule, using 09:00"), raw)
        return 9, 0


def last_run() -> datetime | None:
    try:
        stamp = json.loads(CURRENT_JSON.read_text())["set_at"]
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, KeyError):
        return None


def is_due(cfg: configparser.ConfigParser, now: datetime | None = None) -> bool:
    """Ist heute noch kein Bild geholt worden und die Uhrzeit schon erreicht?"""
    if not cfg["schedule"].getboolean("enabled"):
        return False

    now = now or datetime.now()
    previous = last_run()
    if previous is None:
        return True                      # noch nie gelaufen -> sofort
    if previous.date() >= now.date():
        return False                     # heute schon erledigt

    hour, minute = scheduled_time(cfg)
    return now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def run_watch(query: str | None) -> int:
    """Ersatz für den systemd-Timer innerhalb eines Snaps.

    Snaps dürfen keine Units ins Home des Benutzers schreiben, und
    »daemon-scope: user« ist in snapd standardmäßig abgeschaltet. Deshalb
    läuft hier ein schlanker Prozess, der die Wanduhr abfragt -- das übersteht
    auch Standby, weil jede Runde neu vergleicht statt herunterzuzählen.
    """
    cfg = load_config()
    interval = max(1, cfg["schedule"].getint("check_every")) * 60
    log.info(_("Watch mode started, checking every %d minutes"), interval // 60)

    while True:
        cfg = load_config()              # Einstellungen können sich ändern
        try:
            if is_due(cfg):
                run_update(cfg, query)
            else:
                log.debug("Nothing to do")
        except Exception as err:
            log.error(_("Run failed: %s"), err)
            log.debug("Details:", exc_info=True)
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# Abläufe
# --------------------------------------------------------------------------- #

def run_update(cfg: configparser.ConfigParser, query_override: str | None) -> int:
    ensure_session_bus()

    wallpaper_cfg = cfg["wallpaper"]
    target_dir = expand_path(wallpaper_cfg["directory"])

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
                log.error(_("Unsplash rejected the access key (HTTP %d) -- "
                            "falling back to Lorem Picsum"), err.code)
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

    sync_autostart(cfg)

    log.info(_("Done -- photo by %s (%s)"), photo.author, photo.page_url or photo.source)
    return 0


def show_status(cfg: configparser.ConfigParser) -> int:
    ensure_session_bus()
    rows: list[tuple[str, str]] = []

    rows.append((_("Configuration"), str(CONFIG_FILE)))
    rows.append((_("Log file"), str(LOG_FILE)))
    rows.append((_("Image folder"), str(expand_path(cfg["wallpaper"]["directory"]))))

    key = unquote(cfg["unsplash"]["access_key"])
    rows.append((_("Unsplash key"),
                 _("set ({prefix}...)").format(prefix=key[:6]) if key
                 else _("not set -> Lorem Picsum")))

    try:
        rows.append((_("Currently active"),
                     gsettings("get", "org.gnome.desktop.background", "picture-uri")))
    except RuntimeError as err:
        rows.append((_("Currently active"),
                     _("not readable ({error})").format(error=err)))

    if CURRENT_JSON.exists():
        current = json.loads(CURRENT_JSON.read_text())
        rows.append((_("Last change"),
                     _("{when} -- photo by {author}").format(
                         when=current.get("set_at"), author=current.get("author"))))
        if current.get("page_url"):
            rows.append((_("Source"), current["page_url"]))

    in_snap = bool(os.environ.get("SNAP"))
    if in_snap:
        hour, minute = scheduled_time(cfg)
        state = _("on") if cfg["schedule"].getboolean("enabled") else _("off")
        rows.append((_("Schedule"),
                     _("{state}, daily at {time}").format(
                         state=state, time=f"{hour:02d}:{minute:02d}")))
        rows.append((_("Due now"), _("yes") if is_due(cfg) else _("no")))

    # Breite aus den übersetzten Beschriftungen, sonst verrutscht die Spalte
    width = max(len(label) for label, _value in rows)
    for label, value in rows:
        print(f"{label:<{width}} : {value}")

    if in_snap:
        return 0

    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-timers", f"{APP}.timer", "--no-pager"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as err:
        log.debug("systemctl unavailable: %s", err)
        return 0

    if result.returncode == 0 and result.stdout.strip():
        print("\n" + _("Timer:"))
        print(result.stdout.rstrip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog=APP,
        description=_("Downloads a photo from Unsplash once a day and sets it as the "
                    "GNOME wallpaper."),
    )
    parser.add_argument("--status", action="store_true",
                        help=_("Show current state and timer"))
    parser.add_argument("--query", metavar=_("TERM"),
                        help=_("Search term for this run only"))
    parser.add_argument("--config", action="store_true",
                        help=_("Print the path of the configuration file"))
    parser.add_argument("--if-due", action="store_true",
                        help=_("Only run if no image has been fetched today"))
    parser.add_argument("--watch", action="store_true",
                        help=_("Run in the background and change daily at the configured time"))
    parser.add_argument("-v", "--verbose", action="store_true", help=_("Debug output"))
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config()

    if args.config:
        print(CONFIG_FILE)
        return 0
    if args.status:
        return show_status(cfg)
    if args.watch:
        return run_watch(args.query)

    try:
        if args.if_due and not is_due(cfg):
            log.info(_("Already done today, or the scheduled time has not been reached"))
            return 0
        return run_update(cfg, args.query)
    except Exception as err:
        log.error(_("Aborted: %s"), err)
        log.debug("Details:", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
