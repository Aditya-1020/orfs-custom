"""Fix sky130 fakeram LEF obstruction layers."""
from __future__ import annotations

import re
import sys
from pathlib import Path

BLOCK = ("met1", "met2")


def fix(text: str) -> str:
    m = re.search(r"SIZE\s+([\d.]+)\s+BY\s+([\d.]+)\s*;", text)
    if not m:
        raise ValueError("no SIZE statement")
    w, h = m.group(1), m.group(2)
    out, in_obs, skip, seen = [], False, False, set()
    for ln in text.splitlines():
        t = ln.strip()
        if t == "OBS":
            in_obs, seen = True, set()
        if in_obs:
            lm = re.match(r"LAYER\s+(\S+)\s*;", t)
            if lm:
                skip = lm.group(1) == "OVERLAP"
                if not skip:
                    seen.add(lm.group(1))
            if skip and not lm:
                if t == "END":
                    skip, in_obs = False, False
                    out += add_missing(seen, w, h)
                    out.append(ln)
                continue
            if skip:
                continue
            if t == "END":
                in_obs = False
                out += add_missing(seen, w, h)
        out.append(ln)
    return "\n".join(out) + "\n"


def add_missing(seen, w, h):
    r = []
    for layer in BLOCK:
        if layer not in seen:
            r += [f"    LAYER {layer} ;", f"    RECT 0 0 {w} {h} ;"]
    return r


def fix_file(path: Path) -> str:
    path = Path(path)
    text = path.read_text()
    if "OVERLAP" not in text and all(f"LAYER {l}" in text for l in BLOCK):
        return f"{path}: already ok"
    path.write_text(fix(text))
    return f"{path}: fixed"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    for p in args:
        print(fix_file(Path(p)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
