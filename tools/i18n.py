#!/usr/bin/env python3
"""Übersetzungswerkzeug ohne gettext-Abhängigkeit.

extract  liest _() und ngettext() aus den Quelldateien und schreibt eine .pot
compile  übersetzt .po-Dateien in das binäre .mo-Format

Damit braucht weder der Snap-Bau noch die Desktop-Installation das Paket
gettext. Übersetzer können die .po-Dateien trotzdem mit Poedit bearbeiten.
"""

from __future__ import annotations

import argparse
import ast
import re
import struct
import sys
from pathlib import Path

DOMAIN = "daily-wallpaper"


# --------------------------------------------------------------------------- #
# Extraktion
# --------------------------------------------------------------------------- #

def extract(sources: list[Path]) -> list[tuple[str, str | None, list[str]]]:
    """Sammelt (singular, plural, fundstellen) aus _()- und ngettext()-Aufrufen."""
    found: dict[tuple[str, str | None], list[str]] = {}

    for source in sources:
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            args = [a.value for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name == "_" and len(args) >= 1:
                key = (args[0], None)
            elif name == "ngettext" and len(args) >= 2:
                key = (args[0], args[1])
            else:
                continue
            found.setdefault(key, []).append(f"{source.name}:{node.lineno}")

    return [(s, p, loc) for (s, p), loc in found.items()]


def po_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n").replace("\t", "\\t"))


def write_pot(entries, target: Path) -> None:
    lines = [
        'msgid ""', 'msgstr ""',
        r'"Project-Id-Version: daily-wallpaper\n"',
        r'"MIME-Version: 1.0\n"',
        r'"Content-Type: text/plain; charset=UTF-8\n"',
        r'"Content-Transfer-Encoding: 8bit\n"',
        r'"Plural-Forms: nplurals=2; plural=(n != 1);\n"',
        "",
    ]
    for singular, plural, locations in sorted(entries):
        lines.append(f"#: {' '.join(sorted(locations))}")
        lines.append(f'msgid "{po_escape(singular)}"')
        if plural is None:
            lines.append('msgstr ""')
        else:
            lines.append(f'msgid_plural "{po_escape(plural)}"')
            lines.append('msgstr[0] ""')
            lines.append('msgstr[1] ""')
        lines.append("")
    target.write_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# .po lesen und nach .mo schreiben
# --------------------------------------------------------------------------- #

def unescape(text: str) -> str:
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t", "\\": "\\",
                                       '"': '"'}.get(m.group(1), m.group(1)), text)


def parse_po(path: Path) -> dict[str, list[str]]:
    """Liefert {msgid: [übersetzung, ...]}; bei Plural mehrere Einträge."""
    catalog: dict[str, list[str]] = {}
    msgid = msgid_plural = None
    plurals: dict[int, str] = {}
    field = None
    buffer: list[str] = []

    def flush():
        nonlocal msgid, msgid_plural, plurals, field, buffer
        if field == "msgstr":
            plurals[0] = "".join(buffer)
        elif field and field.startswith("msgstr["):
            plurals[int(field[7])] = "".join(buffer)
        elif field == "msgid":
            msgid = "".join(buffer)
        elif field == "msgid_plural":
            msgid_plural = "".join(buffer)
        buffer = []

    def store():
        nonlocal msgid, msgid_plural, plurals
        if msgid is not None:
            texts = [plurals.get(i, "") for i in range(max(plurals) + 1)] if plurals else []
            if any(texts):
                key = msgid if msgid_plural is None else f"{msgid}\x00{msgid_plural}"
                catalog[key] = texts
        msgid = msgid_plural = None
        plurals = {}

    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r'^(msgid_plural|msgid|msgstr\[\d\]|msgstr)\s+"(.*)"$', line)
        if match:
            if match.group(1) == "msgid":
                flush(); store()
            else:
                flush()
            field = match.group(1)
            buffer = [unescape(match.group(2))]
            continue
        if line.startswith('"') and line.endswith('"'):
            buffer.append(unescape(line[1:-1]))
    flush(); store()
    return catalog


def write_mo(catalog: dict[str, list[str]], header: str, target: Path) -> None:
    """Schreibt das .mo-Format (GNU gettext, ohne Hash-Tabelle)."""
    items: list[tuple[bytes, bytes]] = [(b"", header.encode("utf-8"))]
    for key, texts in sorted(catalog.items()):
        items.append((key.encode("utf-8"), "\x00".join(texts).encode("utf-8")))
    items.sort(key=lambda pair: pair[0])

    count = len(items)
    keys_offset = 7 * 4 + 16 * count
    offsets, keys, values = [], b"", b""
    for key, value in items:
        offsets.append((len(keys), len(key), len(values), len(value)))
        keys += key + b"\x00"
        values += value + b"\x00"
    values_offset = keys_offset + len(keys)

    key_table, value_table = b"", b""
    for k_off, k_len, v_off, v_len in offsets:
        key_table += struct.pack("<II", k_len, keys_offset + k_off)
        value_table += struct.pack("<II", v_len, values_offset + v_off)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        struct.pack("<Iiiiiii", 0x950412DE, 0, count, 7 * 4, 7 * 4 + 8 * count, 0, 0)
        + key_table + value_table + keys + values)


def po_header(path: Path) -> str:
    """Kopf der .po -- enthält Plural-Forms, das gettext zwingend braucht."""
    catalog_lines, inside = [], False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith('msgid ""'):
            inside = True
            continue
        if inside:
            if line.startswith('msgstr ""'):
                continue
            if line.startswith('"') and line.endswith('"'):
                catalog_lines.append(unescape(line[1:-1]))
            elif line and not line.startswith("#"):
                break
    return "".join(catalog_lines)


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    e = sub.add_parser("extract", help="Vorlage po/daily-wallpaper.pot erzeugen")
    e.add_argument("sources", nargs="+", type=Path)
    e.add_argument("--output", type=Path, default=Path("po") / f"{DOMAIN}.pot")

    c = sub.add_parser("compile", help=".po-Dateien nach .mo übersetzen")
    c.add_argument("--po-dir", type=Path, default=Path("po"))
    c.add_argument("--output", type=Path, required=True,
                   help="Zielordner, darunter entsteht <lang>/LC_MESSAGES/")

    args = parser.parse_args()

    if args.command == "extract":
        entries = extract(args.sources)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_pot(entries, args.output)
        print(f"{len(entries)} Meldungen -> {args.output}")
        return 0

    written = 0
    for po in sorted(args.po_dir.glob("*.po")):
        catalog = parse_po(po)
        target = args.output / po.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
        write_mo(catalog, po_header(po), target)
        print(f"{po.name}: {len(catalog)} Übersetzungen -> {target}")
        written += 1
    if not written:
        print("Keine .po-Dateien gefunden", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
