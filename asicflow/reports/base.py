"""Dual-consumable report documents (human text + machine JSON/CSV)."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..tables import emit_table, render_table, write_csv


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


@dataclass
class Finding:
    """One actionable observation for humans and scripts."""

    severity: str  # info | warn | error
    code: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "evidence": _jsonable(self.evidence),
        }


@dataclass
class ReportTable:
    name: str
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "headers": list(self.headers),
            "rows": [[_jsonable(c) for c in r] for r in self.rows],
        }


@dataclass
class Report:
    """Canonical report object written as .json / .txt / optional .csv set."""

    name: str
    title: str
    meta: dict[str, Any] = field(default_factory=dict)
    tables: list[ReportTable] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    sections: dict[str, Any] = field(default_factory=dict)
    generated_at: str = field(default_factory=_now)

    def add_table(
        self,
        name: str,
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        description: str = "",
    ) -> ReportTable:
        table = ReportTable(
            name=name,
            headers=list(headers),
            rows=[list(r) for r in rows],
            description=description,
        )
        self.tables.append(table)
        return table

    def add_finding(
        self,
        severity: str,
        code: str,
        message: str,
        **evidence: Any,
    ) -> Finding:
        f = Finding(severity=severity, code=code, message=message, evidence=evidence)
        self.findings.append(f)
        return f

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "asicflow.report.v1",
            "name": self.name,
            "title": self.title,
            "generated_at": self.generated_at,
            "meta": _jsonable(self.meta),
            "findings": [f.to_dict() for f in self.findings],
            "tables": [t.to_dict() for t in self.tables],
            "sections": _jsonable(self.sections),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True) + "\n"

    def to_text(self) -> str:
        lines: list[str] = [self.title, "=" * len(self.title), ""]
        if self.meta:
            lines.append("## Meta")
            for k in sorted(self.meta):
                lines.append(f"  {k}: {self.meta[k]}")
            lines.append("")
        if self.findings:
            lines.append("## Findings")
            for f in self.findings:
                lines.append(f"  [{f.severity.upper()}] {f.code}: {f.message}")
            lines.append("")
        for table in self.tables:
            desc = f" — {table.description}" if table.description else ""
            lines.append(render_table(table.headers, table.rows, title=f"{table.name}{desc}"))
            lines.append("")
        for key, value in self.sections.items():
            lines.append(f"## {key}")
            if isinstance(value, (dict, list)):
                lines.append(json.dumps(_jsonable(value), indent=2, sort_keys=True))
            else:
                lines.append(str(value))
            lines.append("")
        lines.append(f"generated_at: {self.generated_at}")
        return "\n".join(lines).rstrip() + "\n"

    def write(
        self,
        out_dir: Path | str,
        *,
        basename: str | None = None,
        print_tables: bool = False,
    ) -> dict[str, Path]:
        """Write JSON + text (+ per-table CSV). Returns artifact paths."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        base = basename or self.name
        paths: dict[str, Path] = {}
        json_path = out / f"{base}.json"
        txt_path = out / f"{base}.txt"
        json_path.write_text(self.to_json())
        txt_path.write_text(self.to_text())
        paths["json"] = json_path
        paths["txt"] = txt_path
        for table in self.tables:
            csv_path = out / f"{base}_{table.name}.csv"
            write_csv(csv_path, table.headers, table.rows)
            paths[f"csv:{table.name}"] = csv_path
            if print_tables:
                emit_table(
                    table.headers,
                    table.rows,
                    title=f"{self.title} / {table.name}",
                    csv_path=None,
                )
        return paths


def load_report(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def write_kv_csv(path: Path, rows: Sequence[tuple[str, Any]], *, headers=("key", "value")) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(list(headers))
        for k, v in rows:
            w.writerow([k, "" if v is None else v])
    return path


def delta_rows(
    current: dict[str, Any],
    baseline: dict[str, Any],
    keys: Sequence[str],
) -> list[list[Any]]:
    """Build [metric, baseline, current, abs_delta, pct_delta] rows."""
    out = []
    for key in keys:
        b = baseline.get(key)
        c = current.get(key)
        abs_d = pct = "NA"
        try:
            bf, cf = float(b), float(c)
            abs_d = cf - bf
            pct = ((cf - bf) / abs(bf) * 100.0) if bf != 0 else ("inf" if cf != 0 else 0.0)
        except (TypeError, ValueError):
            pass
        out.append([key, b if b is not None else "NA", c if c is not None else "NA", abs_d, pct])
    return out
