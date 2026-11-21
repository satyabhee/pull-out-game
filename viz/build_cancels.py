"""Inline viz/cancels_tab.js (+ its CSS) into viz/index.html: 8 "Cancel matching" scenes appended to the Explainer. Idempotent.

usage: python -m viz.build_cancels
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
BEGIN, END = "<!-- CANCELS:BEGIN -->", "<!-- CANCELS:END -->"

CSS = """
.cx { height: 100%; overflow: auto; max-width: 1100px; margin: 0 auto; padding: 14px 20px 28px; display: flex; flex-direction: column; gap: 12px; }
.cx-head h1 { font-family: var(--display); font-weight: 500; font-size: 28px; margin: 4px 0 2px; text-wrap: balance; }
.cx-head p { margin: 0; color: var(--mute); max-width: 68ch; }
.cx-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.cx-chip { font-size: 13px; padding: 5px 11px; border-radius: 16px; border: 1px solid var(--line); background: transparent; color: var(--mute); cursor: pointer; display: inline-flex; gap: 7px; align-items: center; }
.cx-chip.on { color: var(--ink); border-color: var(--accent); background: var(--panel); }
.cx-chip:focus-visible, .cx-seg:focus-visible, .cx-ctl button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cx-n { font-family: var(--mono); font-size: 11px; color: #ff6f59; }
.cx-bd { display: flex; flex-direction: column; gap: 4px; }
.cx-bar { display: flex; gap: 3px; height: 26px; }
.cx-seg { min-width: 22px; border: 1px solid #ff6f59; background: rgba(255,111,89,.10); color: #ff6f59; font-family: var(--mono); font-size: 11px; border-radius: 5px; cursor: pointer; padding: 0; }
.cx-seg.on { background: rgba(255,111,89,.32); color: var(--ink); }
.cx-bdcap { margin: 0; font-size: 12px; color: var(--mute); }
.cx-title { font-family: var(--display); font-weight: 500; font-size: 21px; margin: 2px 0 0; text-wrap: balance; }
.cx-cnt { font-family: var(--mono); font-size: 13px; color: #ff6f59; }
.cx-stage { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px; overflow-x: auto; }
.cx-svg { display: block; width: 100%; min-width: 640px; height: auto; }
.cx-lbl { font-family: var(--mono); font-size: 11px; letter-spacing: .1em; fill: var(--mute); }
.cx-tl { font-family: var(--mono); font-size: 15px; font-weight: 500; }
.cx-ts { font-family: var(--mono); font-size: 11px; fill: var(--mute); }
.cx-gl { font-family: var(--body); font-size: 12px; }
.cx-gw { font-family: var(--mono); font-size: 11px; }
.cx-sn { font-family: var(--body); font-size: 15px; font-weight: 600; }
.cx-ss { font-family: var(--body); font-size: 12px; fill: var(--mute); }
.cx-x { font-size: 18px; font-weight: 700; }
.cx-fn { font-family: var(--body); font-size: 13px; font-weight: 600; }
.cx-fill { font-family: var(--mono); font-size: 13px; }
.cx-foot { display: flex; flex-direction: column; gap: 10px; }
.cx-cap { margin: 0; min-height: 4.4em; max-width: 78ch; font-size: 15px; }
.cx-ctl { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.cx-ctl button { font-size: 13px; padding: 6px 14px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel2); color: var(--ink); cursor: pointer; }
.cx-ctl button.pri { border-color: var(--accent); }
.cx-ctl button:disabled { opacity: .4; cursor: default; }
.cx-dots { display: inline-flex; gap: 6px; }
.cx-dots i { width: 8px; height: 8px; border-radius: 50%; background: var(--line); cursor: pointer; }
.cx-dots i.on { background: var(--accent); }
.cx-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px; color: var(--mute); padding-top: 6px; border-top: 1px solid var(--line); }
.cx-legend span { display: inline-flex; align-items: center; gap: 6px; }
.cx-legend i { width: 14px; height: 10px; border: 1.5px solid; border-radius: 3px; display: inline-block; }
.cx-src { font-family: var(--mono); font-size: 11px; margin-left: auto; }
@media (max-width: 640px) { .cx-head h1 { font-size: 23px; } .cx-src { margin-left: 0; } }
"""

PATCHES = [
    ('"Unknown, being measured now: the combined effect of today\'s coverage changes. No estimate."]}];function readHash(){',
     '"Unknown, being measured now: the combined effect of today\'s coverage changes. No estimate."]}].concat(window.CANCEL_SCENES||[]);function readHash(){'),
]


def main() -> int:
    page = HERE / "index.html"
    s = page.read_text()
    s = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", s, flags=re.S)
    js = (HERE / "cancels_tab.js").read_text()
    block = f"{BEGIN}\n<style>{CSS}</style>\n<script>\n{js}\n</script>\n{END}\n"
    anchor = '<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"></script>\n'
    if anchor not in s:
        sys.exit("react-dom script tag not found")
    s = s.replace(anchor, anchor + block, 1)
    for old, new in PATCHES:
        if new in s:
            continue
        if s.count(old) != 1:
            sys.exit(f"patch anchor not found exactly once: {old[:60]}")
        s = s.replace(old, new, 1)
    page.write_text(s)
    print(f"wrote {page} ({len(s)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
