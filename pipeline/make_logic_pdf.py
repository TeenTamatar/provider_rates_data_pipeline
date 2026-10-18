#!/usr/bin/env python3
"""Render LOGIC.md to LOGIC.pdf (python-markdown + headless Edge or Chrome).

    python pipeline/make_logic_pdf.py [--src LOGIC.md] [--out LOGIC.pdf]
"""
import argparse
import os
import re
import shutil
import subprocess
import tempfile

import markdown

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "msedge", "google-chrome", "chromium", "chrome",
]
CSS = """
@page { size: A4; margin: 16mm 14mm; }
body { font-family: 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 9.5pt; line-height: 1.4; color: #1a1a1a; }
h1 { font-size: 20pt; border-bottom: 2px solid #333; padding-bottom: 4px; }
h2 { font-size: 15pt; margin-top: 22px; border-bottom: 1px solid #999; padding-bottom: 2px; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin-top: 16px; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 8.3pt; }
th, td { border: 1px solid #bbb; padding: 3px 5px; vertical-align: top; text-align: left; }
th { background: #eee; }
tr { page-break-inside: avoid; }
code { font-family: Consolas, monospace; font-size: 8.3pt; background: #f2f2f2; padding: 0 2px; word-break: break-word; }
pre { background: #f2f2f2; padding: 6px; overflow-wrap: anywhere; white-space: pre-wrap; }
hr { border: 0; border-top: 1px solid #ccc; }
a { color: #1a4f8b; text-decoration: none; }
"""


def find_browser():
    for b in BROWSERS:
        if os.path.isfile(b) or shutil.which(b):
            return b
    raise SystemExit("no Edge or Chrome found for PDF output")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=os.path.join(ROOT, "LOGIC.md"))
    ap.add_argument("--out", default=os.path.join(ROOT, "LOGIC.pdf"))
    a = ap.parse_args()

    text = open(a.src, encoding="utf-8").read().replace("\\$", "$")
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    html = f"<!doctype html><html><head><meta charset='utf-8'><title>Logic file</title><style>{CSS}</style></head><body>{body}</body></html>"
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "logic.html")
        open(h, "w", encoding="utf-8").write(html)
        out = os.path.abspath(a.out)
        subprocess.run([find_browser(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", "file:///" + h.replace("\\", "/")],
                       check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("wrote", out, os.path.getsize(out) // 1024, "KB")


if __name__ == "__main__":
    main()
