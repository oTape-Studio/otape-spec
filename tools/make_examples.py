#!/usr/bin/env python3
"""Build the example cassettes in examples/files/.

    python3 tools/make_examples.py

Writes complete .otape files with nothing but the Python standard library,
plus `afconvert` (macOS) or `ffmpeg` for the AAC-LC audio. It doubles as a
small reference *writer*: how to lay out the ZIP, the mimetype entry, the
manifest, audio and artwork with transparent keep-out zones.

Part of the oTAPE specification · © 2026 Jan Osio · CC BY 4.0
"""
from __future__ import annotations

import io
import json
import math
import shutil
import struct
import subprocess
import tempfile
import wave
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
G = json.loads((ROOT / "geometry" / "otape-geometry-1.0.json").read_text(encoding="utf-8"))
OUT = ROOT / "examples" / "files"
RATE = 44100
MIME = b"application/vnd.otape.cassette"
# Fixed timestamp so the examples are reproducible.
STAMP = (2026, 10, 29, 12, 0, 0)


# --- tiny PNG writer -----------------------------------------------------------

def png(width: int, height: int, pixel) -> bytes:
    """RGBA PNG; pixel(x, y) -> (r, g, b, a)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixel(x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def hex_rgb(value: str) -> tuple[int, int, int]:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def label_png(per_mm: float, paper: str, accent: str, ink: str) -> bytes:
    """A label with stripes and the keep-out zone (§12.5) left transparent."""
    L, mech = G["label"], G["mechanism"]
    ox, oy = L["origin_on_shell"]
    win, m = mech["window"], L["keep_out_margin"]
    hubs = [(h[0] - ox, h[1] - oy) for h in (mech["left_hub"], mech["right_hub"])]
    r_hole = mech["hub_hole_diameter"] / 2 + m
    wx0, wy0 = win["x"] - ox - m, win["y"] - oy - m
    wx1, wy1 = wx0 + win["width"] + 2 * m, wy0 + win["height"] + 2 * m
    band = L["zones"]["bottom_band"]
    box = L["zones"]["side_letter_box"]
    w, h = round(L["width"] * per_mm), round(L["height"] * per_mm)
    P, A, I = hex_rgb(paper), hex_rgb(accent), hex_rgb(ink)

    def pixel(px: int, py: int):
        x, y = (px + 0.5) / per_mm, (py + 0.5) / per_mm
        if wx0 <= x <= wx1 and wy0 <= y <= wy1 or any((x - hx) ** 2 + (y - hy) ** 2 <= r_hole ** 2 for hx, hy in hubs):
            return (0, 0, 0, 0)
        if y < 2.2:
            return (*A, 255)
        if y >= band["y"]:
            stripe = int((y - band["y"]) / 2.2)
            if (y - band["y"]) % 2.2 < 1.6 and stripe < 4:
                return (*(A if stripe != 2 else I), 255)
        # Outline of the side-letter box.
        bx0, by0 = box["x"], box["y"]
        bx1, by1 = bx0 + box["width"], by0 + box["height"]
        if bx0 <= x <= bx1 and by0 <= y <= by1 and (x - bx0 < 0.3 or bx1 - x < 0.3 or y - by0 < 0.3 or by1 - y < 0.3):
            return (*I, 255)
        for ruled in L["ruled_lines_y"]:
            if abs(y - ruled) < 0.1 and 11.0 <= x <= 86.5:
                return (*I, 90)
        return (*P, 255)

    return png(w, h, pixel)


def cover_png(per_mm: float) -> bytes:
    """A sunset J-card front, 63.5 × 101.6 mm."""
    J = G["jcard"]
    w, h = round(J["front_width"] * per_mm), round(J["height"] * per_mm)

    def pixel(px: int, py: int):
        t = py / h
        sky = (int(28 + 200 * t), int(16 + 60 * t), int(51 + 40 * (1 - t)))
        x, y = px / per_mm, py / per_mm
        if (x - 31.75) ** 2 + (y - 62) ** 2 < 16 ** 2 and int(y) % 3 != 0:
            return (255, 196, 92, 255)
        if y > 70:
            return (24, 14, 40, 255)
        return (*sky, 255)

    return png(w, h, pixel)


# --- audio -------------------------------------------------------------------

def tone_wav(seconds: float, gap: tuple[float, float]) -> bytes:
    """Stereo 16-bit WAV: 440/660 Hz tones with a silent gap, like a paused source."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = bytearray()
        for i in range(int(seconds * RATE)):
            t = i / RATE
            silent = gap[0] <= t < gap[1]
            left = 0 if silent else int(0.5 * 32767 * math.sin(2 * math.pi * 440 * t))
            right = 0 if silent else int(0.25 * 32767 * math.sin(2 * math.pi * 660 * t))
            frames += struct.pack("<hh", left, right)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def encode_aac(wav: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.m4a"
        src.write_bytes(wav)
        if shutil.which("afconvert"):
            cmd = ["afconvert", "-f", "m4af", "-d", "aac", "-b", "256000", str(src), str(dst)]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-loglevel", "error", "-i", str(src), "-c:a", "aac", "-b:a", "256k", str(dst)]
        else:
            raise SystemExit("needs afconvert (macOS) or ffmpeg to encode AAC")
        subprocess.run(cmd, check=True)
        return dst.read_bytes()


# --- container ---------------------------------------------------------------

def write_otape(path: Path, manifest: dict, files: dict[str, bytes]) -> None:
    """mimetype first and stored (§3.2), manifest deflated, media stored."""
    with zipfile.ZipFile(path, "w") as z:
        def add(name: str, data: bytes, compress: bool) -> None:
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)

        add("mimetype", MIME, False)
        add("manifest.json", (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(), True)
        for name, data in files.items():
            add(name, data, not name.endswith((".m4a", ".png", ".jpg")))
    print("wrote", path.relative_to(ROOT))


def blank_c60() -> None:
    manifest = {
        "format": "oTAPE",
        "format_version": "1.0",
        "cassette": {
            "id": "0b1d2c3e-4f50-4a61-8b72-93a4b5c6d7e8",
            "serial_number": "OT-000000",
            "title": "",
            "type": "C60",
            "side_length_seconds": 1800,
            "timeline_rate": RATE,
            "created_at": "2026-10-29T12:00:00Z",
            "created_with": "oTAPE make_examples.py",
        },
        "sides": {
            "A": {"title": "", "write_protect": "intact", "audio": None, "markers": []},
            "B": {"title": "", "write_protect": "intact", "audio": None, "markers": []},
        },
        "appearance": {
            "preset": "ferro",
            "shell": {"color": "#1B1B1D", "finish": "opaque"},
            "label": {"style": "ruled", "paper": "#F3EEDC", "ink": "#1D2B53", "accent": "#C8102E", "font": "marker"},
        },
        "history": [{"type": "created", "timestamp": "2026-10-29T12:00:00Z", "app": "oTAPE make_examples.py"}],
    }
    write_otape(OUT / "blank-c60.otape", manifest, {})


def sample_tone() -> None:
    seconds, gap = 6.0, (3.0, 4.0)
    frames = int(seconds * RATE)
    audio = encode_aac(tone_wav(seconds, gap))
    paper, accent, ink = "#FAFAF5", "#E4572E", "#202020"
    files = {
        "audio/side-a.m4a": audio,
        "artwork/label-a.png": label_png(G["resolution"]["minimum_px_per_mm"], paper, accent, ink),
        "artwork/jcard-front.png": cover_png(G["resolution"]["minimum_px_per_mm"]),
        "docs/booklet.html": (
            "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>Test Tones</title>\n"
            "<style>body{font-family:Georgia,serif;margin:6mm;background:#F4EEDF;color:#1F2A44}"
            "h1{font-size:20pt}</style></head>\n<body><h1>Test Tones</h1>\n"
            "<p>Six seconds: 440 Hz on the left, 660 Hz on the right, with one second of silence "
            "where the source was paused.</p>\n<img src=\"../artwork/jcard-front.png\" alt=\"Cover\" width=\"120\">\n"
            "</body></html>\n").encode(),
        "docs/notes.md": b"# Test Tones\n\nAn example cassette for oTAPE 1.0 implementers.\n",
        "docs/license.txt": (
            "Test Tones, an oTAPE example cassette\n"
            "Copyright (c) 2026 Jan Osio\n\n"
            "Licensed under the Creative Commons Attribution 4.0 International licence (CC BY 4.0).\n"
            "https://creativecommons.org/licenses/by/4.0/\n").encode(),
    }
    manifest = {
        "format": "oTAPE",
        "format_version": "1.0",
        "cassette": {
            "id": "7f8e9d0c-1b2a-4394-8576-a5b4c3d2e1f0",
            "serial_number": "OT-000001",
            "title": "Test Tones",
            "creator": "oTAPE examples",
            "description": "A short reference cassette for testing oTAPE readers.",
            "type": "C46",
            "side_length_seconds": 1380,
            "timeline_rate": RATE,
            "created_at": "2026-10-29T12:00:00Z",
            "modified_at": "2026-10-29T12:05:00Z",
            "created_with": "oTAPE make_examples.py",
            "rights": {
                "copyright": "© 2026 Jan Osio",
                "license": "CC-BY-4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "file": "docs/license.txt",
            },
        },
        "sides": {
            "A": {
                "title": "Tones",
                "write_protect": "broken",
                "audio": {
                    "file": "audio/side-a.m4a",
                    "codec": "aac-lc",
                    "sample_rate": RATE,
                    "channels": 2,
                    "bit_rate": 256000,
                    "recorded_frames": frames,
                    "duration_seconds": seconds,
                },
                "markers": [
                    {"frame": 0, "title": "Tone", "kind": "track"},
                    {"frame": int(gap[0] * RATE), "title": "Pause", "kind": "note"},
                    {"frame": int(gap[1] * RATE), "title": "Tone again", "kind": "track"},
                ],
            },
            "B": {"title": "", "write_protect": "intact", "audio": None, "markers": []},
        },
        "appearance": {
            "preset": "smoke",
            "shell": {"color": "#3A3F478C", "finish": "transparent"},
            "label": {"style": "stripes", "paper": paper, "ink": ink, "accent": accent, "font": "pen"},
        },
        "jcard": {
            "card_color": "#1C1033",
            "ink": "#FFE8A3",
            "font": "stencil",
            "front": {"title": "Test Tones", "subtitle": "oTAPE 1.0 example"},
            "spine": {"text": "TEST TONES · oTAPE"},
            "tracklist": {"A": ["Tone", "Tone again"], "B": []},
            "dedication": "For everyone building an oTAPE player.",
        },
        "artwork": {"label_a": "artwork/label-a.png", "jcard_front": "artwork/jcard-front.png"},
        "booklet": {"html": "docs/booklet.html", "notes": "docs/notes.md"},
        "history": [
            {"type": "created", "timestamp": "2026-10-29T12:00:00Z", "app": "oTAPE make_examples.py"},
            {"type": "recorded", "timestamp": "2026-10-29T12:01:00Z", "side": "A",
             "start_frame": 0, "end_frame": frames, "source": "generated", "source_format": "wav"},
            {"type": "write_protect_changed", "timestamp": "2026-10-29T12:05:00Z", "side": "A", "state": "broken"},
        ],
        "extensions": {"org.otape.examples": {"purpose": "reference"}},
    }
    write_otape(OUT / "sample-tones.otape", manifest, files)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    blank_c60()
    sample_tone()
