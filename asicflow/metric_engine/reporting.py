"""Human-readable metric summaries."""
import csv
import sqlite3
from collections import defaultdict

from ..tables import emit_table

def print_summary(rows):
    design = {(r[0], r[4]): r[5] for r in rows if r[2] == "-" and r[3] in ("design", "net")}
    value = {(r[0], r[2], r[4]): r[5] for r in rows if r[2] != "-" and r[3] in ("timing", "power", "status")}
    keys = sorted({(r[0], r[2]) for r in rows if r[2] != "-" and r[3] == "status" and r[4] == "result"})
    if not keys:
        return

    def get(table, key, scale=1.0, fmt="{:.3f}"):
        try:
            return fmt.format(float(table[key]) * scale)
        except (KeyError, ValueError):
            return "NA"

    headers = ("RUN", "CORNER", "STATUS", "SETUP_WS", "SETUP_TNS", "HOLD_WS", "POWER_MW", "UTIL_%", "HPWL_MM")
    body = [
        (
            run,
            corner,
            value[(run, corner, "result")].upper(),
            get(value, (run, corner, "setup_ws")),
            get(value, (run, corner, "setup_tns")),
            get(value, (run, corner, "hold_ws")),
            get(value, (run, corner, "total.total_W"), 1000),
            get(design, (run, "util_all_pct"), 1, "{:.2f}"),
            get(design, (run, "hpwl_total_um"), 0.001, "{:.2f}"),
        )
        for run, corner in keys
    ]
    print()
    emit_table(headers, body, title="metrics summary")

def export_wide(db_path, out_csv):
    """One row per (platform, run, stage, corner); columns '<category>.<metric>'.
    Design-level rows (corner '-') are repeated on every corner row of that run/stage."""
    con = sqlite3.connect(db_path)
    design, per_corner = defaultdict(dict), defaultdict(dict)
    for plat, run, stage, corner, col, val in con.execute(
            "SELECT platform, run_tag, stage, corner, category||'.'||metric, value "
            "FROM metrics ORDER BY platform, run_tag, stage, corner, category, metric"):
        if corner == "-":
            design[(plat, run, stage)][col] = val
        else:
            per_corner[(plat, run, stage, corner)][col] = val
    con.close()
    table = [{"platform": k[0], "run_tag": k[1], "stage": k[2], "corner": k[3],
              **design.get(k[:3], {}), **v} for k, v in sorted(per_corner.items())]
    columns = sorted({c for row in table for c in row} - {"platform", "run_tag", "stage", "corner"})
    with open(out_csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, ["platform", "run_tag", "stage", "corner"] + columns)
        writer.writeheader()
        writer.writerows(table)
    print(f"WIDE: {out_csv}  ({len(table)} rows x {len(columns)} metric columns)")
