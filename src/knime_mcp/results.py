"""Locating and reading the files a workflow produces.

KNIME nodes write their results to disk (CSV, Excel, JSON, images, ...).  This
module finds files created by an execution and previews tabular ones without
pulling in pandas -- ``csv`` and ``json`` are stdlib, and ``xlsx`` is a zip of
XML we can unpack ourselves.
"""

from __future__ import annotations

import csv
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

#: Files KNIME itself writes inside a workflow; never interesting as output.
_INTERNAL_NAMES = {
    "workflow.knime",
    "workflow.svg",
    "workflow-metadata.xml",
    "workflowset.meta",
    "knime.log",
    "settings.xml",
    "settings.svg",
}
_INTERNAL_DIRS = {".artifacts", ".savedWithData", "internal", "dialog", ".metadata"}


@dataclass
class OutputFile:
    path: Path
    size: int
    modified: float
    suffix: str

    def to_dict(self) -> dict:
        from datetime import datetime, timezone

        return {
            "path": str(self.path),
            "name": self.path.name,
            "size": self.size,
            "modified": datetime.fromtimestamp(self.modified, tz=timezone.utc).isoformat(),
            "suffix": self.suffix,
        }


def find_outputs(
    roots: list[Path],
    since: Optional[float] = None,
    limit: int = 200,
) -> list[OutputFile]:
    """Files under ``roots`` created/modified after ``since`` (epoch seconds)."""
    results: list[OutputFile] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for item in sorted(root.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(root)
            if any(part in _INTERNAL_DIRS or part.startswith(".") for part in relative.parts[:-1]):
                continue
            if relative.name in _INTERNAL_NAMES or relative.name.startswith("."):
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            if since is not None and stat.st_mtime < since - 1.0:
                continue
            resolved = item.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            results.append(
                OutputFile(path=item, size=stat.st_size, modified=stat.st_mtime, suffix=item.suffix.lower())
            )
    results.sort(key=lambda f: f.modified, reverse=True)
    return results[:limit]


# -- tabular previews ---------------------------------------------------------

def read_table(
    path: Path,
    max_rows: int = 50,
    max_chars: int = 20_000,
    sheet: Optional[str] = None,
) -> dict:
    """Preview a table-ish file as ``{"columns": [...], "rows": [[...]]}``."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        delimiter = "\t" if suffix == ".tsv" else ","
        return _read_delimited(path, delimiter, max_rows, max_chars)
    if suffix in (".json", ".jsonl", ".ndjson"):
        return _read_json(path, max_rows, max_chars)
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(path, max_rows, max_chars, sheet)
    if suffix in (".knime",):
        return {
            "error": (
                "KNIME's native table format cannot be read directly. "
                "Write the result with an Excel Writer or a CSV/JSON writer node instead."
            ),
            "path": str(path),
        }
    return {"error": f"unsupported file type '{suffix}'", "path": str(path)}


def _truncate(rows: list[list[Any]], max_rows: int, max_chars: int) -> list[list[Any]]:
    out = rows[:max_rows]
    while out and len(json.dumps(out, default=str)) > max_chars:
        out.pop()
    return out


def _read_delimited(path: Path, delimiter: str, max_rows: int, max_chars: int) -> dict:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=delimiter + ";|")
            delimiter = dialect.delimiter
        except csv.Error:
            pass
        reader = csv.reader(fh, delimiter=delimiter)
        rows = [list(row) for row in reader]
    if not rows:
        return {"columns": [], "rows": [], "row_count": 0, "path": str(path)}
    columns = rows[0]
    body = rows[1:]
    kept = _truncate(body, max_rows, max_chars)
    return {
        "columns": columns,
        "rows": kept,
        "row_count": len(body),
        "truncated": len(kept) < len(body),
        "path": str(path),
    }


def _read_json(path: Path, max_rows: int, max_chars: int) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        records = json.loads(text)

    if isinstance(records, dict):
        # single record, or {"data": [...]}-style wrapper
        for key in ("data", "rows", "records", "results"):
            if isinstance(records.get(key), list):
                records = records[key]
                break
        else:
            records = [records]
    if not isinstance(records, list):
        records = [records]

    columns: list[str] = []
    for record in records[:200]:
        if isinstance(record, dict):
            for key in record:
                if key not in columns:
                    columns.append(key)
    body = [
        [record.get(col) if isinstance(record, dict) else record for col in columns]
        for record in records
    ]
    kept = _truncate(body, max_rows, max_chars)
    return {
        "columns": columns,
        "rows": kept,
        "row_count": len(body),
        "truncated": len(kept) < len(body),
        "path": str(path),
    }


_SHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _column_index(ref: str) -> int:
    match = _CELL_REF.match(ref)
    if not match:
        return 0
    letters = match.group(1)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _read_xlsx(path: Path, max_rows: int, max_chars: int, sheet: Optional[str]) -> dict:
    """Minimal xlsx reader: shared strings + the first (or named) worksheet."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _SHEET_NS):
                shared.append("".join(t.text or "" for t in si.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))

        sheet_files = sorted(
            n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        target = sheet_files[0] if sheet_files else None
        if sheet:
            target = _sheet_by_name(zf, sheet) or target
        if not target:
            return {"error": "no worksheet found", "path": str(path)}

        root = ET.fromstring(zf.read(target))
        rows_out: list[list[Any]] = []
        for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
            values: dict[int, Any] = {}
            for cell in row.findall("m:c", _SHEET_NS):
                ref = cell.get("r") or ""
                col = _column_index(ref) if ref else len(values)
                ctype = cell.get("t", "n")
                if ctype == "inlineStr":
                    node = cell.find("m:is", _SHEET_NS)
                    value = "".join(t.text or "" for t in node.iter(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")) if node is not None else ""
                else:
                    v = cell.find("m:v", _SHEET_NS)
                    raw = v.text if v is not None and v.text is not None else ""
                    if ctype == "s":
                        value = shared[int(raw)] if raw != "" else ""
                    elif ctype == "b":
                        value = raw == "1"
                    elif ctype == "str":
                        value = raw
                    else:
                        try:
                            value = float(raw) if "." in raw or "e" in raw.lower() else int(raw)
                        except ValueError:
                            value = raw
                values[col] = value
            if values:
                width = max(values) + 1
                rows_out.append([values.get(i) for i in range(width)])
        sheet_name = target.rsplit("/", 1)[-1]

    if not rows_out:
        return {"columns": [], "rows": [], "row_count": 0, "sheet": sheet_name, "path": str(path)}
    header = [str(c) if c is not None else "" for c in rows_out[0]]
    width = len(header)
    body = [row + [None] * (width - len(row)) for row in rows_out[1:]]
    kept = _truncate(body, max_rows, max_chars)
    return {
        "sheet": sheet_name,
        "columns": header,
        "rows": kept,
        "row_count": len(body),
        "truncated": len(kept) < len(body),
        "path": str(path),
    }


def _sheet_by_name(zf: zipfile.ZipFile, name: str) -> Optional[str]:
    """Resolve a human sheet name to ``xl/worksheets/sheetN.xml``."""
    if "xl/workbook.xml" not in zf.namelist():
        return None
    try:
        root = ET.fromstring(zf.read("xl/workbook.xml"))
    except ET.ParseError:
        return None
    ns = _SHEET_NS
    sheets = root.find("m:sheets", ns)
    if sheets is None:
        return None
    for index, entry in enumerate(sheets, start=1):
        if entry.get("name") == name:
            # r:id ordering matches sheetN.xml in files written by Excel/Knime
            for candidate in (f"xl/worksheets/sheet{index}.xml",):
                if candidate in zf.namelist():
                    return candidate
    return None


def describe_file(path: Path) -> dict:
    """Header info for any output file (used when preview is impossible)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    stat = path.stat()
    info: dict[str, Any] = {
        "path": str(path),
        "size": stat.st_size,
        "suffix": path.suffix.lower(),
    }
    if path.suffix.lower() in (".csv", ".tsv", ".txt", ".log", ".json", ".xml", ".html"):
        with open(path, "rb") as fh:
            head = fh.read(4096)
        info["head"] = head.decode("utf-8", errors="replace")
    return info
