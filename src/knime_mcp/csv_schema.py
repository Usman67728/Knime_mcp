"""Build a CSV Reader `spec` block from a real file's header + inferred types.

Mirrors what the KNIME CSV Table Reader does on scan: one `spec/N` per column
with name, KNIME type name and hasType=true.  Column names come from the header
row (or generated names when the file has no header).  Types are inferred by
scanning up to `scan_rows` data rows.
"""
from __future__ import annotations

import csv
from pathlib import Path

# KNIME DataType class names used in the spec `type` entries
KT_STRING = "java.lang.String"
KT_INT = "java.lang.Integer"
KT_LONG = "java.lang.Long"
KT_DOUBLE = "java.lang.Double"

# label + cell-factory signature for each inferred type
_LABELS = {
    KT_STRING: ("String",
                "org.knime.core.data.def.StringCell$StringCellFactory"
                ".createCell(class java.lang.String)"),
    KT_INT: ("Number (Integer)",
             "org.knime.core.data.def.IntCell$IntCellFactory.create(class java.lang.Integer)"),
    KT_LONG: ("Number (Long)",
              "org.knime.core.data.def.LongCell$LongCellFactory.create(class java.lang.Long)"),
    KT_DOUBLE: ("Number (Double)",
                "org.knime.core.data.def.DoubleCell$DoubleCellFactory.create(class java.lang.Double)"),
}


def production_for(ktype: str) -> tuple[str, str]:
    """Return ``(type_label, cell_factory)`` for an inferred KNIME type."""
    return _LABELS.get(ktype, _LABELS[KT_STRING])


# bare converter names, matching the ``_converter_name`` entries KNIME writes
KT_TO_NAME = {
    KT_STRING: "String",
    KT_INT: "Integer",
    KT_LONG: "Long",
    KT_DOUBLE: "Double",
}


def _infer(values: list[str]) -> str:
    """Infer a KNIME type from sample cell strings (String/Integer/Long/Double)."""
    seen: set[str] = set()
    for v in values:
        v = v.strip()
        if v == "":
            continue  # skip missing values
        seen.add(v)
    if not seen:
        return KT_STRING
    try:
        for s in seen:
            int(s)
        return KT_INT
    except ValueError:
        pass
    try:
        for s in seen:
            float(s)
        # prefer Long for whole numbers beyond int range
        try:
            for s in seen:
                int(s)
            return KT_LONG
        except ValueError:
            return KT_DOUBLE
    except ValueError:
        return KT_STRING


def build_spec(path: Path, delimiter: str = ",", first_row_header: bool = True,
               scan_rows: int = 100) -> tuple[list[str], list[str]]:
    """Return (column_names, knime_types) for ``path``."""
    text = path.read_text(encoding="utf-8", errors="replace")
    sample = "\n".join(text.splitlines()[: scan_rows + 1])
    rows = list(csv.reader(sample.splitlines(), delimiter=delimiter))
    if not rows:
        return [], []
    header = rows[0] if first_row_header else None
    data = rows[1:] if first_row_header else rows
    width = max(len(r) for r in rows)
    if header and len(header) == width:
        names = [h.strip() for h in header]
    else:
        names = [f"Column{i}" for i in range(width)]
    types: list[str] = []
    for i in range(width):
        col_vals = [r[i] for r in data if len(r) > i]
        types.append(_infer(col_vals))
    return names, types