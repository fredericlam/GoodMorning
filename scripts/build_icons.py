#!/usr/bin/env python3
"""Embarque les icônes Phosphor (MIT, https://phosphoricons.com) utilisées par le dashboard.

Usage : npm pack @phosphor-icons/core && tar xzf phosphor-icons-core-*.tgz
        python scripts/build_icons.py package/
Réécrit le bloc ICONS:BEGIN/END de dashboard/index.html (style duotone, sauf suffixe « -fill »).
"""
import json
import re
import sys
from pathlib import Path

ICONS = [
    # matières
    "math-operations", "book-open-text", "globe-hemisphere-west", "translate", "plant", "flask", "sneaker-move",
    "palette", "headphones", "laptop", "columns", "scales", "lightbulb", "chart-line-up", "chats-teardrop",
    "pencil-simple-line", "mask-happy", "microphone-stage", "sparkle",
    # interface
    "sparkle-fill", "moon", "sun", "backpack", "note-pencil", "fire", "confetti", "moon-stars", "calendar-heart",
    "island", "flower-lotus", "puzzle-piece", "lock-simple", "flower-tulip", "x-circle", "pencil-simple",
    "book-open", "paperclip", "check-circle", "upload-simple", "warning-circle", "coffee", "bowl-food", "cloud",
    "sun-horizon", "house-line", "alarm", "star", "push-pin", "chat-circle", "smiley-wink", "drop",
    # onglets famille
    "envelope-simple", "envelope-simple-open", "paper-plane-tilt", "file-text", "file-pdf", "certificate", "receipt",
    "signature", "download-simple", "magnifying-glass", "folder-simple", "eye", "graduation-cap",
    "identification-card", "briefcase", "user-circle", "arrow-square-out", "x", "tray", "shield-check", "seal-check",
    "hourglass-medium", "arrow-left", "image", "files", "clipboard-text", "stamp",
]

pkg = Path(sys.argv[1] if len(sys.argv) > 1 else "package")
out = {}
for name in ICONS:
    weight, base = ("fill", name[:-5]) if name.endswith("-fill") else ("duotone", name)
    svg = (pkg / "assets" / weight / f"{base}-{weight}.svg").read_text()
    out[name] = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1).strip()

html = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
src = html.read_text()
block = ("/* ICONS:BEGIN — généré par scripts/build_icons.py · Phosphor Icons 2.x (MIT) */\n"
         f"const ICONS = {json.dumps(out, indent=0, ensure_ascii=False)};\n/* ICONS:END */")
new, n = re.subn(r"/\* ICONS:BEGIN.*?/\* ICONS:END \*/", lambda _: block, src, flags=re.S)
if n != 1:
    sys.exit("bloc ICONS:BEGIN/END introuvable dans dashboard/index.html")
html.write_text(new)
print(f"{len(out)} icônes → {html}")
