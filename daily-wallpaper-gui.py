#!/usr/bin/env python3
"""GTK4/libadwaita-Oberfläche für daily-wallpaper.

Die eigentliche Logik liegt in daily-wallpaper.py und wird hier nur
angesteuert -- es gibt keine zweite Implementierung.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "de.zurek.UnsplashWallpaper"
UNIT = "daily-wallpaper"

# Muss zur »version« in snap/snapcraft.yaml passen.
VERSION = "1.1"

DEVELOPER = "dav Apps"
WEBSITE_URL = "https://dav-apps.tech"
REPO_URL = "https://github.com/Dav2070/daily-wallpaper"
PRIVACY_URL = "https://dav-apps.tech/privacy"
DONATE_URL = "https://buy.stripe.com/eVq9AUaRb8defnldt5c7u02"
UNSPLASH_URL = "https://unsplash.com"
PICSUM_URL = "https://picsum.photos"


# --------------------------------------------------------------------------- #
# Kernmodul laden (Dateiname enthält Bindestriche -> kein normaler Import)
# --------------------------------------------------------------------------- #

def load_core():
    here = Path(__file__).resolve()
    candidates = [
        here.with_name("daily-wallpaper.py"),         # Projektordner
        here.with_name("daily-wallpaper"),            # Snap: bin/daily-wallpaper
        Path.home() / ".local/bin/daily-wallpaper",
    ]
    if snap := os.environ.get("SNAP"):
        candidates.insert(0, Path(snap) / "bin/daily-wallpaper")
    for path in candidates:
        if path.exists():
            loader = importlib.machinery.SourceFileLoader("uw_core", str(path))
            spec = importlib.util.spec_from_loader("uw_core", loader)
            module = importlib.util.module_from_spec(spec)
            # Muss vor exec_module registriert sein, sonst findet @dataclass
            # das eigene Modul nicht (sys.modules-Lookup in dataclasses).
            sys.modules["uw_core"] = module
            loader.exec_module(module)
            return module
    raise SystemExit("daily-wallpaper.py not found")


core = load_core()
core.setup_logging(verbose=False)

_ = core._
ngettext = core.ngettext


# --------------------------------------------------------------------------- #
# Datumsangaben
# --------------------------------------------------------------------------- #

# Über gettext übersetzt statt über das Locale, damit die Anzeige nicht davon
# abhängt, mit welchem LANG die Anwendung gestartet wurde.
def weekday_name(moment: datetime) -> str:
    return (_("Monday"), _("Tuesday"), _("Wednesday"), _("Thursday"),
            _("Friday"), _("Saturday"), _("Sunday"))[moment.weekday()]


def month_name(moment: datetime) -> str:
    return (_("January"), _("February"), _("March"), _("April"), _("May"),
            _("June"), _("July"), _("August"), _("September"), _("October"),
            _("November"), _("December"))[moment.month - 1]


def format_day(moment: datetime) -> str:
    """»today«, »tomorrow«, »on Tuesday«, »on 5 September«.

    Die Platzhalter stehen im Übersetzungstext, damit jede Sprache
    Wortstellung und Zeichensetzung selbst bestimmt -- Deutsch braucht etwa
    »am 5. September« mit Punkt.
    """
    delta = (moment.date() - date.today()).days
    if delta == 0:
        return _("today")
    if delta == -1:
        return _("yesterday")
    if delta == 1:
        return _("tomorrow")
    if delta == -2:
        return _("the day before yesterday")
    if 1 < delta < 7:
        return _("on {weekday}").format(weekday=weekday_name(moment))
    if -7 < delta < -1:
        return _("last {weekday}").format(weekday=weekday_name(moment))
    if moment.year == date.today().year:
        return _("on {day} {month}").format(day=moment.day, month=month_name(moment))
    return _("on {day} {month} {year}").format(
        day=moment.day, month=month_name(moment), year=moment.year)


def format_when(moment: datetime) -> str:
    """»today at 09:03«, »on 5 September at 18:45«."""
    return _("{day} at {time}").format(day=format_day(moment), time=f"{moment:%H:%M}")


def format_relative(moment: datetime) -> str:
    """Kurz zurückliegende Zeitpunkte als Abstand, ältere mit Datum."""
    seconds = (datetime.now() - moment).total_seconds()
    if seconds < 0:
        return format_when(moment)
    if seconds < 90:
        return _("just now")

    minutes = int(seconds // 60)
    if minutes < 60:
        return ngettext("{count} minute ago", "{count} minutes ago",
                        minutes).format(count=minutes)

    hours = int(seconds // 3600)
    if hours < 12 and moment.date() == date.today():
        return ngettext("{count} hour ago", "{count} hours ago",
                        hours).format(count=hours)
    return format_when(moment)


def format_caption(moment: datetime) -> str:
    """Kurzform fürs Verlaufsraster.

    Ohne Präposition, anders als format_day: unter einer Kachel steht
    »17. August«, nicht »am 17. August«. Bei heute und gestern zusätzlich die
    Uhrzeit, weil an einem Tag mehrere Bilder geladen worden sein können.
    """
    delta = (moment.date() - date.today()).days
    if delta == 0:
        day = _("today")
    elif delta == -1:
        day = _("yesterday")
    elif delta == -2:
        day = _("the day before yesterday")
    elif -7 < delta < 0:
        day = weekday_name(moment)
    elif moment.year == date.today().year:
        day = _("{day} {month}").format(day=moment.day, month=month_name(moment))
    else:
        day = _("{day} {month} {year}").format(
            day=moment.day, month=month_name(moment), year=moment.year)

    day = day[0].upper() + day[1:]      # bei Ziffern wirkungslos
    if delta in (0, -1):
        return _("{day}, {time}").format(day=day, time=f"{moment:%H:%M}")
    return day


def parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# config.ini zeilenweise bearbeiten -- erhält Kommentare und Reihenfolge
# --------------------------------------------------------------------------- #

def write_setting(section: str, key: str, value: str) -> None:
    path = core.CONFIG_FILE
    lines = path.read_text().splitlines(keepends=True)
    current, replaced, insert_at = None, False, None

    for index, line in enumerate(lines):
        header = re.match(r"\s*\[([^\]]+)\]", line)
        if header:
            if current == section and insert_at is None:
                insert_at = index          # Ende des Zielabschnitts erreicht
            current = header.group(1)
            continue
        if current == section and re.match(rf"\s*{re.escape(key)}\s*=", line):
            lines[index] = f"{key} = {value}\n"
            replaced = True
            break

    if not replaced:
        if insert_at is None:
            if current != section:
                lines.append(f"\n[{section}]\n")
            insert_at = len(lines)
        lines.insert(insert_at, f"{key} = {value}\n")

    path.write_text("".join(lines))


# --------------------------------------------------------------------------- #
# systemd-Timer
# --------------------------------------------------------------------------- #

class ConfigSchedule:
    """Zeitplan über die Konfigurationsdatei, ausgewertet von »--watch«.

    Wird im Snap benutzt: Snaps dürfen keine systemd-Units ins Home schreiben,
    und »daemon-scope: user« ist in snapd standardmäßig abgeschaltet.
    """

    @staticmethod
    def installed() -> bool:
        return True

    @staticmethod
    def enabled() -> bool:
        return core.load_config()["schedule"].getboolean("enabled")

    @staticmethod
    def scheduled_time() -> tuple[int, int]:
        return core.scheduled_time(core.load_config())

    @classmethod
    def next_run(cls) -> str:
        if not cls.enabled():
            return _("Schedule is off")

        last = core.last_run()
        if last is None:
            return _("First run pending")

        hour, minute = cls.scheduled_time()
        moment = datetime.now().replace(hour=hour, minute=minute,
                                        second=0, microsecond=0)
        if last.date() >= moment.date() or moment < datetime.now():
            moment += timedelta(days=1)

        text = _("Next run {when}").format(when=format_when(moment))
        if last:
            text += "   ·   " + _("last {when}").format(when=format_relative(last))
        return text

    @staticmethod
    def set_enabled(enabled: bool) -> None:
        write_setting("schedule", "enabled", "true" if enabled else "false")
        core.set_autostart(enabled)

    @staticmethod
    def set_time(hour: int, minute: int) -> None:
        write_setting("schedule", "time", f"{hour:02d}:{minute:02d}")

    @staticmethod
    def run_now() -> None:
        pass


class SystemdTimer:
    UNIT_DIR = Path.home() / ".config/systemd/user"

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=20)

    @classmethod
    def installed(cls) -> bool:
        return (cls.UNIT_DIR / f"{UNIT}.timer").exists()

    @classmethod
    def enabled(cls) -> bool:
        return cls._run("is-enabled", f"{UNIT}.timer").stdout.strip() == "enabled"

    @classmethod
    def _timestamp(cls, prop: str) -> datetime | None:
        """Liest eine systemd-Zeiteigenschaft als datetime.

        --timestamp=unix liefert »@1788678185« und umgeht damit das Parsen der
        lokalisierten Textform.
        """
        result = cls._run("show", f"{UNIT}.timer", "-p", prop,
                          "--value", "--timestamp=unix")
        value = result.stdout.strip()
        if value.startswith("@") and value[1:].isdigit() and value != "@0":
            return datetime.fromtimestamp(int(value[1:]))
        return None

    @classmethod
    def next_run(cls) -> str:
        if not cls.installed():
            return _("Schedule not installed")
        if not cls.enabled():
            return _("Schedule is off")

        moment = cls._timestamp("NextElapseUSecRealtime")
        text = (_("Next run {when}").format(when=format_when(moment)) if moment
                else _("Next run unknown"))

        last = cls._timestamp("LastTriggerUSec")
        if last:
            text += "   ·   " + _("last {when}").format(when=format_relative(last))
        return text

    @classmethod
    def scheduled_time(cls) -> tuple[int, int]:
        unit_file = cls.UNIT_DIR / f"{UNIT}.timer"
        if unit_file.exists():
            match = re.search(r"(?m)^OnCalendar=.*?(\d{1,2}):(\d{2})", unit_file.read_text())
            if match:
                return int(match.group(1)), int(match.group(2))
        return 9, 0

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        cls._run("enable" if enabled else "disable", "--now", f"{UNIT}.timer")

    @classmethod
    def set_time(cls, hour: int, minute: int) -> None:
        unit_file = cls.UNIT_DIR / f"{UNIT}.timer"
        if not unit_file.exists():
            return
        text = re.sub(r"(?m)^OnCalendar=.*$",
                      f"OnCalendar=*-*-* {hour:02d}:{minute:02d}", unit_file.read_text())
        unit_file.write_text(text)
        cls._run("daemon-reload")
        if cls.enabled():
            cls._run("restart", f"{UNIT}.timer")

    @classmethod
    def run_now(cls) -> None:
        cls._run("start", f"{UNIT}.service")


# Im Snap gibt es keine systemd-Units des Benutzers.
Timer = ConfigSchedule if os.environ.get("SNAP") else SystemdTimer


# --------------------------------------------------------------------------- #
# Hauptfenster
# --------------------------------------------------------------------------- #

ORIENTATIONS = [("landscape", _("Landscape")), ("portrait", _("Portrait")),
                ("squarish", _("Square"))]
FITTINGS = [("zoom", _("Zoom (fills the screen)")), ("scaled", _("Scaled")),
            ("centered", _("Centered")), ("stretched", _("Stretched")),
            ("spanned", _("Spanned across monitors"))]


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title(_("Daily Wallpaper"))
        self.set_default_size(880, 700)
        self.busy = False
        self._save_timeouts: dict[str, int] = {}

        self.cfg = core.load_config()
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._build_current(), "current",
                                        _("Current"), "preferences-desktop-wallpaper-symbolic")
        self.stack.add_titled_with_icon(self._build_history(), "history",
                                        _("History"), "view-grid-symbolic")
        self.stack.add_titled_with_icon(self._build_settings(), "settings",
                                        _("Settings"), "preferences-system-symbolic")

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack,
                                                 policy=Adw.ViewSwitcherPolicy.WIDE))

        menu = Gio.Menu()
        menu.append(_("Open image folder"), "win.open-folder")
        menu.append(_("Show log"), "win.open-log")
        menu.append(_("About"), "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        toolbar.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.stack, reveal=False))
        self.toasts.set_child(toolbar)

        for name, handler in (("open-folder", self.on_open_folder),
                              ("open-log", self.on_open_log),
                              ("about", self.on_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        core.sync_autostart(self.cfg)

        self.refresh_current()
        self.refresh_history()
        GLib.timeout_add_seconds(30, self._tick)

    # ----------------------------------------------------------------- Seite 1
    def _build_current(self) -> Gtk.Widget:
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER,
                                   hexpand=True, vexpand=True)
        frame = Gtk.Frame(child=self.picture, overflow=Gtk.Overflow.HIDDEN)
        frame.add_css_class("card")
        frame.set_size_request(-1, 300)

        self.credit = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER,
                                margin_top=12)
        self.credit.add_css_class("heading")
        # Beschreibungen von Unsplash sind oft mehrere Sätze lang -- auf zwei
        # Zeilen begrenzen, damit die Zeitangabe darunter sichtbar bleibt.
        self.subtitle = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER,
                                  lines=2, ellipsize=Pango.EllipsizeMode.END,
                                  max_width_chars=72)
        self.subtitle.add_css_class("dim-label")

        self.taken = Gtk.Label(label="", margin_top=6)
        self.taken.add_css_class("caption")
        self.taken.add_css_class("dim-label")

        self.query_entry = Gtk.SearchEntry(
            placeholder_text=_("Search term for this run (empty = use settings)"),
            hexpand=True)
        self.query_entry.connect("activate", lambda *_: self.fetch())

        self.spinner = Gtk.Spinner()
        content = Adw.ButtonContent(label=_("New image"), icon_name="view-refresh-symbolic")
        self.fetch_button = Gtk.Button(child=content)
        self.fetch_button.add_css_class("suggested-action")
        self.fetch_button.add_css_class("pill")
        self.fetch_button.connect("clicked", lambda *_: self.fetch())

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin_top=18)
        controls.append(self.query_entry)
        controls.append(self.spinner)
        controls.append(self.fetch_button)

        self.schedule_label = Gtk.Label(label="", margin_top=14)
        self.schedule_label.add_css_class("dim-label")
        self.schedule_label.add_css_class("caption")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                      margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        box.append(frame)
        box.append(self.credit)
        box.append(self.subtitle)
        box.append(self.taken)
        box.append(controls)
        box.append(self.schedule_label)

        clamp = Adw.Clamp(maximum_size=760, child=box)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    # ----------------------------------------------------------------- Seite 2
    def _build_history(self) -> Gtk.Widget:
        # min_children_per_line ist nötig, weil Gtk.Picture die volle Bildbreite
        # als natürliche Breite meldet -- sonst landet ein Bild pro Zeile.
        self.flowbox = Gtk.FlowBox(
            valign=Gtk.Align.START, min_children_per_line=3, max_children_per_line=5,
            homogeneous=True, column_spacing=12, row_spacing=12,
            selection_mode=Gtk.SelectionMode.NONE,
            margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)

        self.history_empty = Adw.StatusPage(
            icon_name="image-x-generic-symbolic", title=_("No images yet"),
            description=_("Fetch your first wallpaper on the “Current” page."))

        self.history_stack = Gtk.Stack()
        self.history_stack.add_named(
            Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=1000, child=self.flowbox)), "grid")
        self.history_stack.add_named(self.history_empty, "empty")
        return self.history_stack

    # ----------------------------------------------------------------- Seite 3
    def _build_settings(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        unsplash = self.cfg["unsplash"]
        wallpaper = self.cfg["wallpaper"]

        # --- Unsplash ---
        group = Adw.PreferencesGroup(
            title=_("Unsplash"),
            description=_("Without an access key, Lorem Picsum is used as a "
                          "fallback source."))

        self.key_row = Adw.PasswordEntryRow(title=_("Access key"))
        self.key_row.set_text(core.unquote(unsplash["access_key"]))
        self.key_row.connect("changed", self._debounced, "unsplash", "access_key")
        group.add(self.key_row)

        self.query_row = Adw.EntryRow(title=_("Search terms (comma separated)"))
        self.query_row.set_text(unsplash["query"])
        self.query_row.connect("changed", self._debounced, "unsplash", "query")
        group.add(self.query_row)

        self.collections_row = Adw.EntryRow(title=_("Collection IDs (take precedence)"))
        self.collections_row.set_text(unsplash["collections"])
        self.collections_row.connect("changed", self._debounced, "unsplash", "collections")
        group.add(self.collections_row)

        self.orientation_row = self._combo(_("Orientation"), ORIENTATIONS,
                                           unsplash["orientation"], "unsplash", "orientation")
        group.add(self.orientation_row)
        page.add(group)

        # --- Hintergrundbild ---
        group = Adw.PreferencesGroup(title=_("Wallpaper"))

        self.fit_row = self._combo(_("Scaling"), FITTINGS,
                                   wallpaper["picture_options"], "wallpaper", "picture_options")
        group.add(self.fit_row)

        self.keep_row = Adw.SpinRow.new_with_range(0, 100, 1)
        self.keep_row.set_title(_("Images to keep"))
        self.keep_row.set_subtitle(_("Older ones are deleted. 0 = keep all"))
        self.keep_row.set_value(wallpaper.getint("keep"))
        self.keep_row.connect("notify::value", lambda row, _:
                              write_setting("wallpaper", "keep", str(int(row.get_value()))))
        group.add(self.keep_row)

        self.notify_row = Adw.SwitchRow(title=_("Show notification"),
                                        subtitle=_("Naming the photographer"))
        self.notify_row.set_active(wallpaper.getboolean("notify"))
        self.notify_row.connect("notify::active", lambda row, _:
                                write_setting("wallpaper", "notify",
                                              "true" if row.get_active() else "false"))
        group.add(self.notify_row)

        folder_row = Adw.ActionRow(title=_("Image folder"),
                                   subtitle=str(core.expand_path(wallpaper["directory"])))
        button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", self.on_open_folder)
        folder_row.add_suffix(button)
        group.add(folder_row)
        page.add(group)

        # --- Zeitplan ---
        # Im Snap gibt es keinen systemd-Timer -- die Erklärung muss zum
        # tatsächlich benutzten Mechanismus passen.
        if os.environ.get("SNAP"):
            schedule_hint = _("Handled by a background service that starts with "
                              "your session and catches up a missed day.")
        else:
            schedule_hint = _("Run by a systemd timer, and caught up if the "
                              "computer was switched off at that time.")
        group = Adw.PreferencesGroup(title=_("Schedule"), description=schedule_hint)

        hour, minute = Timer.scheduled_time()
        self.timer_row = Adw.SwitchRow(title=_("Change automatically every day"))
        self.timer_row.set_active(Timer.installed() and Timer.enabled())
        self.timer_row.set_sensitive(Timer.installed())
        if not Timer.installed():
            self.timer_row.set_subtitle(_("Timer not installed -- run ./install.sh"))
        elif os.environ.get("SNAP"):
            self.timer_row.set_subtitle(_("Run by the background service of the snap"))
        self.timer_row.connect("notify::active", self.on_timer_toggled)
        group.add(self.timer_row)

        self.hour_row = Adw.SpinRow.new_with_range(0, 23, 1)
        self.hour_row.set_title(_("Hour"))
        self.hour_row.set_value(hour)
        self.hour_row.set_sensitive(Timer.installed())
        self.hour_row.connect("notify::value", self.on_time_changed)
        group.add(self.hour_row)

        self.minute_row = Adw.SpinRow.new_with_range(0, 59, 5)
        self.minute_row.set_title(_("Minute"))
        self.minute_row.set_value(minute)
        self.minute_row.set_sensitive(Timer.installed())
        self.minute_row.connect("notify::value", self.on_time_changed)
        group.add(self.minute_row)
        page.add(group)
        return page

    def _combo(self, title, options, value, section, key) -> Adw.ComboRow:
        model = Gtk.StringList()
        for _, label in options:
            model.append(label)
        row = Adw.ComboRow(title=title, model=model)
        keys = [k for k, _ in options]
        row.set_selected(keys.index(value) if value in keys else 0)
        row.connect("notify::selected", lambda r, _:
                    write_setting(section, key, keys[r.get_selected()]))
        return row

    def _debounced(self, entry, section, key) -> None:
        """Speichert Texteingaben erst, wenn 600 ms nichts mehr getippt wurde."""
        token = f"{section}.{key}"
        if token in self._save_timeouts:
            GLib.source_remove(self._save_timeouts[token])

        def save():
            write_setting(section, key, entry.get_text().strip())
            self._save_timeouts.pop(token, None)
            return GLib.SOURCE_REMOVE

        self._save_timeouts[token] = GLib.timeout_add(600, save)

    # ------------------------------------------------------------ Aktualisieren
    def _tick(self) -> bool:
        self.schedule_label.set_text(Timer.next_run())
        return GLib.SOURCE_CONTINUE

    def refresh_current(self) -> None:
        self.schedule_label.set_text(Timer.next_run())
        if not core.CURRENT_JSON.exists():
            self.credit.set_text(_("No wallpaper set yet"))
            self.subtitle.set_text(_("Click “New image”."))
            self.taken.set_text("")
            return
        data = json.loads(core.CURRENT_JSON.read_text())
        path = Path(data.get("path", ""))
        if path.exists():
            self.picture.set_filename(str(path))
        self.credit.set_text(_("Photo by {author}").format(
            author=data.get("author") or _("Unknown")))
        self.subtitle.set_text(data.get("description") or "")
        self.subtitle.set_visible(bool(data.get("description")))

        moment = parse_stamp(data.get("set_at", ""))
        self.taken.set_text(_("Set {when}").format(when=format_relative(moment))
                            if moment else "")

    def refresh_history(self) -> None:
        while (child := self.flowbox.get_first_child()) is not None:
            self.flowbox.remove(child)

        directory = core.expand_path(self.cfg["wallpaper"]["directory"])
        images = sorted(directory.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if directory.exists() else []

        self.history_stack.set_visible_child_name("grid" if images else "empty")
        for image in images:
            self.flowbox.append(self._thumbnail(image))

    def _thumbnail(self, path: Path) -> Gtk.Widget:
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        picture.set_size_request(-1, 130)
        frame = Gtk.Frame(child=picture, overflow=Gtk.Overflow.HIDDEN)
        frame.add_css_class("card")

        moment = datetime.fromtimestamp(path.stat().st_mtime)
        author = core.load_history().get(path.name, {}).get("author")

        tooltip = [_("Photo by {author}").format(author=author)] if author else []
        tooltip += [_("Downloaded {when}").format(when=format_when(moment)),
                    _("Click to set")]
        button = Gtk.Button(child=frame, hexpand=True,
                            tooltip_text="\n".join(tooltip))
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: self.apply_existing(path))

        caption = Gtk.Label(label=format_caption(moment),
                            ellipsize=Pango.EllipsizeMode.END)
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")

        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        cell.append(button)
        cell.append(caption)

        def load():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), 320, 200, True)
                GLib.idle_add(picture.set_pixbuf, pixbuf)
            except Exception:
                pass

        threading.Thread(target=load, daemon=True).start()
        return cell

    # --------------------------------------------------------------- Aktionen
    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.fetch_button.set_sensitive(not busy)
        self.query_entry.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        (self.spinner.start if busy else self.spinner.stop)()

    def fetch(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        query = self.query_entry.get_text().strip() or None

        def work():
            try:
                self.cfg = core.load_config()          # Einstellungen frisch einlesen
                core.run_update(self.cfg, query)
                GLib.idle_add(self._fetch_done, None)
            except Exception as err:
                GLib.idle_add(self._fetch_done, err)

        threading.Thread(target=work, daemon=True).start()

    def _fetch_done(self, error: Exception | None) -> bool:
        self._set_busy(False)
        if error:
            self.toast(_("Failed: {error}").format(error=error))
        else:
            self.refresh_current()
            self.refresh_history()
            self.toast(_("New wallpaper set"))
        return GLib.SOURCE_REMOVE

    def apply_existing(self, path: Path) -> None:
        try:
            core.ensure_session_bus()
            core.set_wallpaper(path, core.unquote(self.cfg["wallpaper"]["picture_options"]))
            os.utime(path)                              # nach vorne im Verlauf
            # Metadaten des gewählten Bildes, nicht die des zuletzt geladenen
            data = core.load_history().get(path.name, {"author": "Unbekannt"})
            data = {**data, "path": str(path),
                    "set_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            core.CURRENT_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            self.refresh_current()
            self.toast(_("Set: {name}").format(name=path.name))
        except Exception as err:
            self.toast(_("Failed: {error}").format(error=err))

    def on_timer_toggled(self, row, _param) -> None:
        Timer.set_enabled(row.get_active())
        self.schedule_label.set_text(Timer.next_run())
        self.toast(_("Schedule enabled") if row.get_active()
                   else _("Schedule disabled"))

    def on_time_changed(self, *_args) -> None:
        Timer.set_time(int(self.hour_row.get_value()), int(self.minute_row.get_value()))
        self.schedule_label.set_text(Timer.next_run())

    def on_open_folder(self, *_args) -> None:
        directory = core.expand_path(self.cfg["wallpaper"]["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher(file=Gio.File.new_for_path(str(directory))).launch(self, None, None)

    def on_open_log(self, *_args) -> None:
        if core.LOG_FILE.exists():
            Gtk.FileLauncher(file=Gio.File.new_for_path(str(core.LOG_FILE))).launch(self, None, None)
        else:
            self.toast(_("No log available yet"))

    def _debug_info(self) -> str:
        """Systemangaben für Fehlerberichte -- bewusst unübersetzt, damit sie
        in einem Issue ohne Rückfragen lesbar sind."""
        key = core.unquote(self.cfg["unsplash"]["access_key"])
        lines = [
            f"Daily Wallpaper {VERSION}",
            f"Package: {'snap' if os.environ.get('SNAP') else 'system'}",
            f"Python: {sys.version.split()[0]}",
            f"GTK: {Gtk.get_major_version()}.{Gtk.get_minor_version()}."
            f"{Gtk.get_micro_version()}",
            f"libadwaita: {Adw.get_major_version()}.{Adw.get_minor_version()}."
            f"{Adw.get_micro_version()}",
            f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', 'unknown')}"
            f" ({os.environ.get('XDG_SESSION_TYPE', 'unknown')})",
            f"Language: {os.environ.get('LANG', 'unset')}",
            f"Image source: {'Unsplash' if key else 'Lorem Picsum (no access key)'}",
            f"Config: {core.CONFIG_FILE}",
            f"Images: {core.expand_path(self.cfg['wallpaper']['directory'])}",
            f"Log: {core.LOG_FILE}",
        ]
        return "\n".join(lines)

    def on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name=_("Daily Wallpaper"),
            application_icon="daily-wallpaper" if os.environ.get("SNAP") else APP_ID,
            developer_name=DEVELOPER,
            version=VERSION,
            comments=_("Sets a photo from Unsplash as the wallpaper every day."),
            website=WEBSITE_URL,
            issue_url=f"{REPO_URL}/issues",
            copyright=f"© {date.today().year} dav",
            license_type=Gtk.License.MIT_X11)

        about.add_link(_("Source code"), REPO_URL)
        about.add_link(_("Privacy policy"), PRIVACY_URL)
        about.add_link(_("Donate"), DONATE_URL)

        # Die Fotos kommen nicht von dieser Anwendung -- die Quellen gehören
        # sichtbar in den Dialog, nicht nur in die Beschreibung.
        about.add_acknowledgement_section(
            _("Photos"), [f"Unsplash {UNSPLASH_URL}", f"Lorem Picsum {PICSUM_URL}"])
        about.add_legal_section(
            _("Photos"), None, Gtk.License.CUSTOM,
            _("Photos are provided by Unsplash and Lorem Picsum and remain "
              "subject to their own licences. This application is not "
              "affiliated with Unsplash Inc."))

        about.set_debug_info(self._debug_info())
        about.set_debug_info_filename("daily-wallpaper-debug.txt")
        about.present(self)


class Application(Adw.Application):
    def __init__(self):
        # Im Snap darf die Anwendung ihren DBus-Namen nicht besitzen: dafür
        # bräuchte es einen dbus-Slot, und der zieht eine manuelle Prüfung im
        # Store nach sich. NON_UNIQUE verzichtet von vornherein darauf, statt
        # bei jedem Start an einer AppArmor-Ablehnung zu scheitern. Preis ist
        # die Einzelinstanz -- ein zweiter Start öffnet ein zweites Fenster.
        flags = (Gio.ApplicationFlags.NON_UNIQUE if os.environ.get("SNAP")
                 else Gio.ApplicationFlags.DEFAULT_FLAGS)
        super().__init__(application_id=APP_ID, flags=flags)

    def do_activate(self):
        window = self.props.active_window or Window(application=self)
        window.present()


if __name__ == "__main__":
    sys.exit(Application().run(sys.argv))
