"""Terminal + CSV tables (stdlib only)."""
from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Sequence

_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _cell(v: Any) -> str:
    if v is None:
        return "NA"
    if isinstance(v, float):
        # Compact float: up to 4 decimals, strip trailing zeros.
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s if s else "0"
    s = str(v)
    if _NUM_RE.match(s):
        try:
            f = float(s)
            if "." in s or "e" in s.lower():
                s = f"{f:.4f}".rstrip("0").rstrip(".")
        except ValueError:
            pass
    return s


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(list(headers))
        for r in rows:
            w.writerow([_cell(c) for c in r])
    return path


def _term_cols() -> int:
    try:
        return max(40, shutil.get_terminal_size(fallback=(120, 24)).columns)
    except OSError:
        return 120


def render_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str | None = None,
    max_width: int | None = None,
) -> str:
    """ASCII table. Shrinks padding / drops low-priority trailing cols to fit."""
    hdr = [str(h) for h in headers]
    body = [list(map(_cell, r)) for r in rows]
    # Pad short rows so zip stays aligned.
    width_n = len(hdr)
    body = [r + ["NA"] * (width_n - len(r)) if len(r) < width_n else r[:width_n] for r in body]

    # Drop trailing columns until the table fits (keep at least 4 cols).
    limit = _term_cols() if max_width is None else max_width
    use_hdr = hdr
    use_body = body
    while True:
        data = [use_hdr] + use_body
        widths = [max(len(row[i]) for row in data) for i in range(len(use_hdr))]
        # "| " + " | ".join(...) + " |"
        total = sum(widths) + 3 * len(widths) + 1
        if total <= limit or len(use_hdr) <= 4:
            break
        use_hdr = use_hdr[:-1]
        use_body = [r[:-1] for r in use_body]

    numeric = []
    for i in range(len(use_hdr)):
        vals = [row[i] for row in use_body]
        numeric.append(bool(vals) and all(v == "NA" or _NUM_RE.match(v) for v in vals))

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt(row: list[str], *, header: bool = False) -> str:
        cells = []
        for i, c in enumerate(row):
            if header or not numeric[i]:
                cells.append(c.ljust(widths[i]))
            else:
                cells.append(c.rjust(widths[i]))
        return "| " + " | ".join(cells) + " |"

    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append(sep)
    lines.append(fmt(use_hdr, header=True))
    lines.append(sep)
    for row in use_body:
        lines.append(fmt(row))
    lines.append(sep)
    if len(use_hdr) < len(hdr):
        dropped = ", ".join(hdr[len(use_hdr) :])
        lines.append(f"(truncated cols for width={limit}: {dropped})")
    return "\n".join(lines)


def emit_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str | None = None,
    csv_path: Path | str | None = None,
    print_table: bool = True,
    max_width: int | None = None,
) -> str:
    row_list = [list(r) for r in rows]
    # CSV always gets the full column set; terminal may truncate.
    if csv_path is not None:
        write_csv(Path(csv_path), headers, row_list)
    text = render_table(headers, row_list, title=title, max_width=max_width)
    if print_table:
        print(text, flush=True)
        if csv_path is not None:
            print(f"csv → {csv_path}", flush=True)
    return text
