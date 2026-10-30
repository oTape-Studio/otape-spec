#!/usr/bin/env python3
"""Validate oTAPE 1.0 cassettes (.otape) and manifests (manifest.json).

    python3 tools/otape_validate.py my-mixtape.otape
    python3 tools/otape_validate.py examples/manifests/manifest-full.json
    python3 tools/otape_validate.py --quiet examples/files/*.otape

Checks the container (§3), identification (§4), the manifest against the JSON
Schema when the `jsonschema` package is installed, the cross-field rules of
§16.1, and artwork proportions and resolution (§12) read from the PNG/JPEG
headers. Exit status: 0 = valid, 1 = invalid, 2 = usage error.

Only the Python standard library is required.

Part of the oTAPE specification · © 2026 Jan Osio · CC BY 4.0
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA_PATH = ROOT / "schema" / "otape-manifest-1.0.schema.json"
GEOMETRY_PATH = ROOT / "geometry" / "otape-geometry-1.0.json"

MIME = b"application/vnd.otape.cassette"
MAX_MANIFEST = 1 << 20
MAX_IMAGE_PX = 8192
STANDARD_TYPES = {"C46": 1380, "C60": 1800, "C90": 2700, "C120": 3600}
CODECS_REQUIRED = {"aac-lc"}
CODECS_OPTIONAL = {"alac", "flac", "pcm"}
WRITE_PROTECT = {"intact", "broken", "taped_over"}
TIMELINE_RATES = {44100, 48000}
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
COLOR = re.compile(r"^#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


class Report:
    def __init__(self, name: str):
        self.name = name
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, section: str, message: str) -> None:
        self.errors.append(f"[{section}] {message}")

    def warn(self, section: str, message: str) -> None:
        self.warnings.append(f"[{section}] {message}")

    @property
    def valid(self) -> bool:
        return not self.errors


# --- helpers -----------------------------------------------------------------

def safe_path(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    if path in ("manifest.json", "mimetype"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size from a PNG or JPEG header, without decoding the image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            i += 2 + length
    return None


def load_geometry() -> dict:
    with open(GEOMETRY_PATH, encoding="utf-8") as f:
        return json.load(f)


# --- container ---------------------------------------------------------------

def check_container(z: zipfile.ZipFile, r: Report) -> None:
    infos = z.infolist()
    if not infos:
        r.error("§3", "archive is empty")
        return
    first = infos[0]
    if first.filename != "mimetype":
        r.warn("§3.2", "the first entry should be 'mimetype'")
    else:
        if first.compress_type != zipfile.ZIP_STORED:
            r.warn("§3.2", "'mimetype' should be stored (not compressed)")
        if first.extra:
            r.warn("§3.2", "'mimetype' should have no extra field")
        if z.read(first) != MIME:
            r.error("§3.2", f"'mimetype' must contain exactly {MIME.decode()}")
    if "mimetype" not in z.namelist():
        r.warn("§3.2", "no 'mimetype' entry (writers MUST write one)")

    if len(infos) > 1024:
        r.warn("§3.5", f"{len(infos)} entries; should not exceed 1024")
    for info in infos:
        name = info.filename
        if info.flag_bits & 0x1:
            r.error("§3.1", f"{name}: entries must not be encrypted")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            r.error("§3.1", f"{name}: compression method {info.compress_type} is not stored or deflate")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            r.error("§3.1", f"{name}: symbolic links are not allowed")
        if name.endswith("/"):
            continue
        if name not in ("mimetype", "manifest.json") and not safe_path(name):
            r.error("§3.3", f"unsafe entry name: {name!r}")
        ext = name.rsplit(".", 1)[-1].lower()
        if ext in ("m4a", "flac", "wav", "png", "jpg", "jpeg") and info.compress_type != zipfile.ZIP_STORED:
            r.warn("§3.1", f"{name}: audio and images should be stored, not deflated")


# --- manifest ----------------------------------------------------------------

def check_schema(manifest: dict, r: Report) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        r.warn("schema", "jsonschema not installed; structural schema check skipped (pip install jsonschema)")
        return
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    for e in sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in e.path) or "(root)"
        r.error("schema", f"{where}: {e.message}")


def check_manifest(m: object, r: Report, archive: zipfile.ZipFile | None) -> None:
    if not isinstance(m, dict):
        r.error("§6", "manifest root must be a JSON object")
        return
    check_schema(m, r)

    if m.get("format") != "oTAPE":
        r.error("§6.1", "format must be \"oTAPE\"")
    version = m.get("format_version")
    match = re.fullmatch(r"(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if not match:
        r.error("§15.1", "format_version must be \"MAJOR.MINOR\"")
    elif match.group(1) != "1":
        r.error("§15.1", f"major version {match.group(1)} is not covered by this validator (1.x)")

    cassette = m.get("cassette")
    if not isinstance(cassette, dict):
        r.error("§6.2", "cassette is missing")
        return
    try:
        uuid.UUID(str(cassette.get("id")))
    except ValueError:
        r.error("§5.2", "cassette.id must be a UUID")
    created = cassette.get("created_at")
    if not (isinstance(created, str) and RFC3339.match(created)):
        r.error("§5.2", "cassette.created_at must be an RFC 3339 timestamp")
    else:
        try:
            datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            r.error("§5.2", f"cassette.created_at is not a real date: {created}")

    ctype = cassette.get("type")
    side_seconds = cassette.get("side_length_seconds")
    if ctype in STANDARD_TYPES:
        if side_seconds != STANDARD_TYPES[ctype]:
            r.error("§5.1", f"{ctype} requires side_length_seconds = {STANDARD_TYPES[ctype]}, got {side_seconds}")
    elif ctype == "custom":
        if not isinstance(side_seconds, int) or not 1 <= side_seconds <= 5400:
            r.error("§5.1", "custom cassettes need side_length_seconds between 1 and 5400")
    else:
        r.error("§5.1", f"unknown cassette type {ctype!r}")
    rate = cassette.get("timeline_rate", 44100)
    if rate not in TIMELINE_RATES:
        r.error("§5.3", f"timeline_rate must be 44100 or 48000, got {rate}")
        rate = 44100
    side_frames = side_seconds * rate if isinstance(side_seconds, int) else None

    sides = m.get("sides")
    if not isinstance(sides, dict):
        r.error("§7", "sides is missing")
        sides = {}
    for label in ("A", "B"):
        side = sides.get(label)
        if not isinstance(side, dict):
            r.error("§7.1", f"sides.{label} is missing")
            continue
        if side.get("write_protect") not in WRITE_PROTECT:
            r.error("§8.1", f"sides.{label}.write_protect must be intact, broken or taped_over")
        if "audio" not in side:
            r.warn("§7.1", f"sides.{label}.audio should be written explicitly (null for a blank side)")
        audio = side.get("audio")
        if audio is not None:
            check_audio(label, audio, side_frames, r, archive)
        markers = side.get("markers", [])
        if isinstance(markers, list):
            frames = [mk.get("frame") for mk in markers if isinstance(mk, dict)]
            if any(not isinstance(f, int) or f < 0 for f in frames):
                r.error("§7.4", f"sides.{label}.markers: frame must be a non-negative integer")
            elif frames != sorted(frames):
                r.warn("§7.4", f"sides.{label}.markers should be sorted by frame")
            elif side_frames is not None and frames and frames[-1] > side_frames:
                r.error("§7.4", f"sides.{label}.markers: a marker lies beyond the end of the side")

    check_appearance(m, r)
    check_artwork(m, r, archive)
    check_history(m, r, side_frames)
    check_rights(cassette, r, archive)

    ext = m.get("extensions", {})
    if isinstance(ext, dict):
        for key in ext:
            if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", key):
                r.error("§15.3", f"extension key {key!r} must be a reverse-DNS identifier")


def check_audio(label: str, audio: object, side_frames: int | None, r: Report,
                archive: zipfile.ZipFile | None) -> None:
    where = f"sides.{label}.audio"
    if not isinstance(audio, dict):
        r.error("§7.2", f"{where} must be an object or null")
        return
    path = audio.get("file")
    if not safe_path(path):
        r.error("§3.3", f"{where}.file is not a safe archive path: {path!r}")
    elif archive is not None and path not in archive.namelist():
        r.error("§16.1", f"{where}.file {path} is not in the archive")
    codec = audio.get("codec")
    if codec in CODECS_OPTIONAL:
        r.warn("§7.3", f"{where}.codec {codec} is optional; not every player can play it")
    elif codec not in CODECS_REQUIRED:
        r.error("§7.3", f"{where}.codec {codec!r} is not an oTAPE 1.0 codec")
    frames = audio.get("recorded_frames")
    if not isinstance(frames, int) or frames < 0:
        r.error("§7.2", f"{where}.recorded_frames must be a non-negative integer")
    elif side_frames is not None and frames > side_frames:
        r.error("§16.1", f"{where}.recorded_frames {frames} exceeds the side length ({side_frames} frames)")
    if audio.get("channels") not in (None, 1, 2):
        r.error("§7.2", f"{where}.channels must be 1 or 2")
    if codec == "aac-lc" and isinstance(audio.get("bit_rate"), int) and audio["bit_rate"] < 192000:
        r.warn("§7.3", f"{where}.bit_rate below the recommended 192 kb/s")


def check_appearance(m: dict, r: Report) -> None:
    def colors(obj: object, prefix: str) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("color", "paper", "ink", "accent", "card_color") and not (isinstance(value, str) and COLOR.match(value)):
                    r.error("§10.2", f"{prefix}{key} must be #RRGGBB or #RRGGBBAA")
                colors(value, f"{prefix}{key}.")
    colors(m.get("appearance"), "appearance.")
    colors(m.get("jcard"), "jcard.")


def check_artwork(m: dict, r: Report, archive: zipfile.ZipFile | None) -> None:
    geometry = load_geometry()
    slots = geometry["artwork_slots"]
    res = geometry["resolution"]
    entries: list[tuple[str, str, dict | None]] = []
    artwork = m.get("artwork", {}) or {}
    if isinstance(artwork, dict):
        for name, path in artwork.items():
            entries.append((f"artwork.{name}", path, slots.get(name)))
    booklet = m.get("booklet", {}) or {}
    for i, path in enumerate(booklet.get("panels", []) if isinstance(booklet, dict) else []):
        entries.append((f"booklet.panels[{i}]", path, slots["booklet_panel"]))
    if len(booklet.get("panels", []) if isinstance(booklet, dict) else []) > 32:
        r.warn("§3.5", "more than 32 booklet panels")
    for key in ("html", "notes"):
        path = booklet.get(key) if isinstance(booklet, dict) else None
        if path is not None:
            entries.append((f"booklet.{key}", path, None))

    for where, path, slot in entries:
        if not safe_path(path):
            r.error("§3.3", f"{where} is not a safe archive path: {path!r}")
            continue
        if archive is None:
            continue
        if path not in archive.namelist():
            r.error("§16.1", f"{where}: {path} is not in the archive")
            continue
        if slot is None:
            if where == "booklet.html":
                check_html(archive.read(path).decode("utf-8", "replace"), r)
            continue
        data = archive.read(path)
        size = image_size(data)
        if size is None:
            r.error("§12.1", f"{where}: {path} is not a PNG or JPEG image")
            continue
        w, h = size
        if max(w, h) > MAX_IMAGE_PX:
            r.error("§3.5", f"{where}: {w} × {h} px exceeds {MAX_IMAGE_PX} px")
        if len(data) > 32 << 20:
            r.warn("§3.5", f"{where}: larger than 32 MiB")
        if slot.get("square"):
            if w != h:
                r.error("§12.2", f"{where}: thumbnail must be square, is {w} × {h}")
            elif w < slot["minimum_px"]:
                r.warn("§12.2", f"{where}: {w} px is below the {slot['minimum_px']} px minimum")
            continue
        expected = slot["width"] / slot["height"]
        actual = w / h
        if abs(actual / expected - 1) > res["aspect_tolerance"]:
            r.error("§12.1", f"{where}: aspect {actual:.4f} differs from {expected:.4f} "
                             f"({slot['width']} × {slot['height']} mm) by more than 1 %")
            continue
        min_w = round(slot["width"] * res["minimum_px_per_mm"])
        min_h = round(slot["height"] * res["minimum_px_per_mm"])
        if w < min_w or h < min_h:
            r.warn("§12.1", f"{where}: {w} × {h} px is below the minimum {min_w} × {min_h} px (12 px/mm)")


def check_rights(cassette: dict, r: Report, archive: zipfile.ZipFile | None) -> None:
    rights = cassette.get("rights")
    if rights is None:
        return
    if not isinstance(rights, dict):
        r.error("§6.4", "cassette.rights must be an object")
        return
    license_ = rights.get("license")
    if license_ is not None:
        tokens = re.findall(r"[()]|[^\s()]+", license_) if isinstance(license_, str) else []
        words = [t for t in tokens if t not in ("(", ")", "AND", "OR", "WITH")]
        if license_ != "all-rights-reserved" and (not words or not all(
                re.fullmatch(r"[A-Za-z0-9.+-]+", t) for t in words)):
            r.warn("§6.4", f"rights.license {license_!r} does not look like an SPDX expression")
        elif isinstance(license_, str) and "LicenseRef-" in license_ and "file" not in rights:
            r.warn("§6.4", "a LicenseRef- licence should come with rights.file")
    path = rights.get("file")
    if path is None:
        return
    if not safe_path(path):
        r.error("§3.3", f"rights.file is not a safe archive path: {path!r}")
        return
    if not path.lower().endswith((".txt", ".md")):
        r.error("§6.4", f"rights.file must be plain text or Markdown, not {path}")
        return
    if archive is None:
        return
    if path not in archive.namelist():
        r.error("§16.1", f"rights.file: {path} is not in the archive")
        return
    try:
        archive.read(path).decode("utf-8")
    except UnicodeDecodeError:
        r.error("§6.4", f"rights.file: {path} is not valid UTF-8")


def check_html(html: str, r: Report) -> None:
    lowered = html.lower()
    if "<script" in lowered or "javascript:" in lowered or re.search(r"\son[a-z]+\s*=", lowered):
        r.error("§12.7", "booklet.html must not contain scripts or event handlers")
    for tag in ("<iframe", "<object", "<embed", "<form"):
        if tag in lowered:
            r.error("§12.7", f"booklet.html must not contain {tag}>")
    if re.search(r"""(src|href)\s*=\s*["']?\s*(https?:|//)""", lowered):
        r.error("§12.7", "booklet.html must only use relative URLs into the archive")


def check_history(m: dict, r: Report, side_frames: int | None) -> None:
    history = m.get("history", [])
    if not isinstance(history, list):
        return
    previous = None
    for i, event in enumerate(history):
        if not isinstance(event, dict):
            r.error("§13.1", f"history[{i}] must be an object")
            continue
        ts = event.get("timestamp")
        if not (isinstance(ts, str) and RFC3339.match(ts)):
            r.error("§13.1", f"history[{i}].timestamp must be RFC 3339")
            continue
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if previous and when < previous:
            r.warn("§13.1", f"history[{i}] is older than the event before it")
        previous = when
        kind = event.get("type")
        if kind == "recorded":
            start, end = event.get("start_frame"), event.get("end_frame")
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end):
                r.error("§13.2", f"history[{i}]: recorded needs 0 ≤ start_frame ≤ end_frame")
            elif side_frames is not None and end > side_frames:
                r.error("§13.2", f"history[{i}]: end_frame lies beyond the end of the side")
        if kind == "write_protect_changed" and event.get("state") == "intact":
            r.error("§8.2", f"history[{i}]: a tab can never return to intact")


# --- entry point -------------------------------------------------------------

def validate(path: Path) -> Report:
    r = Report(str(path))
    if path.suffix == ".json":
        try:
            manifest = json.loads(path.read_bytes())
        except (OSError, ValueError) as e:
            r.error("§6", f"cannot read JSON: {e}")
            return r
        check_manifest(manifest, r, archive=None)
        return r
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        r.error("§3", f"not a ZIP container: {e}")
        return r
    with z:
        check_container(z, r)
        if "manifest.json" not in z.namelist():
            r.error("§3.4", "manifest.json is missing")
            return r
        info = z.getinfo("manifest.json")
        if info.file_size > MAX_MANIFEST:
            r.error("§3.5", "manifest.json exceeds 1 MiB")
            return r
        raw = z.read(info)
        if raw.startswith(b"\xef\xbb\xbf"):
            r.error("§6", "manifest.json must not start with a byte-order mark")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            r.error("§6", f"manifest.json is not valid UTF-8 JSON: {e}")
            return r
        check_manifest(manifest, r, archive=z)
    return r


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate oTAPE 1.0 files.")
    parser.add_argument("paths", nargs="+", type=Path, help=".otape files or manifest .json files")
    parser.add_argument("--quiet", action="store_true", help="only print the verdict per file")
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args()

    failed = False
    for path in args.paths:
        if not path.exists():
            print(f"{path}: no such file", file=sys.stderr)
            return 2
        r = validate(path)
        ok = r.valid and not (args.strict and r.warnings)
        failed |= not ok
        print(f"{'VALID  ' if ok else 'INVALID'} {path}"
              + (f"  ({len(r.errors)} errors, {len(r.warnings)} warnings)" if r.errors or r.warnings else ""))
        if not args.quiet:
            for e in r.errors:
                print(f"  error    {e}")
            for w in r.warnings:
                print(f"  warning  {w}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
