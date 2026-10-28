"""Harvest node templates from every configured node in the workspace.

Walks the KNIME workspace for ``settings.xml`` files, groups them by node
factory, keeps the **smallest** instance per factory (fewest cached
artefacts -- transformation specs, hilite maps, file store ids ...), and
writes a reusable template folder into ``src/knime_mcp/templates/``.

    python scripts/harvest_all.py                 # harvest everything
    python scripts/harvest_all.py --only csv_reader excel_writer
    python scripts/harvest_all.py --list          # show candidates, harvest nothing

Curated metadata (params / path handling / post-set values) lives in
``CURATED`` below; anything else is harvested bare and remains usable
through free-form ``overrides`` in create_workflow.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knime_mcp import xmlconfig as xc  # noqa: E402
from knime_mcp.templates import harvest, save_template  # noqa: E402

DEFAULT_WORKSPACE = Path(r"C:\Users\chusm\knime-workspace")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src" / "knime_mcp" / "templates"

_FACTORY_RE = re.compile(r'<entry key="factory" type="xstring" value="([^"]+)"')
_NODENAME_RE = re.compile(r'<entry key="node-name"[^>]*value="([^"]*)"')
_BUNDLE_RE = re.compile(r'<entry key="node-bundle-symbolic-name"[^>]*value="([^"]*)"')

# --- curated metadata ---------------------------------------------------------
# One entry per factory: template id, human description, ports and -- where we
# inspected the real settings -- named params.  Paths must exist in the
# harvested file or rendering raises, so only verified paths belong here.

CSV_READER = "org.knime.base.node.io.filehandling.csv.reader.CSVTableReaderNodeFactory"
EXCEL_WRITER = "org.knime.ext.poi3.node.io.filehandling.excel.writer.ExcelTableWriterNodeFactory"
TABLE_WRITER = "org.knime.base.node.io.filehandling.table.writer.TableWriterNodeFactory"
COLUMN_FILTER = "org.knime.base.node.preproc.filter.column.DataColumnSpecFilterNodeFactory"
ROW_FILTER = "org.knime.base.node.preproc.filter.row3.RowFilterNodeFactory"
SORTER = "org.knime.base.node.preproc.sorter.SorterNodeFactory"
COLUMN_RENAMER = "org.knime.base.node.preproc.column.renamer.ColumnRenamerNodeFactory"
GROUPBY = "org.knime.base.node.preproc.groupby.GroupByNodeFactory"

CURATED: dict[str, dict] = {
    CSV_READER: {
        "id": "csv_reader",
        "description": "Read a delimited text file (CSV/TSV) from disk.",
        "inputs": [],
        "outputs": ["table"],
        "path_params": ["path"],
        "post_set": [
            {"path": "model.csvReaderParameters.multiFileSelectionParams.source.path.file_system_type",
             "value": "LOCAL"},
            {"path": "model.csvReaderParameters.multiFileSelectionParams.source.path.location_present",
             "value": True},
        ],
        # the harvested file's cached column specs belong to *its* source file
        "clear": ["model.transformationParameters"],
        "params": [
            {"name": "path", "type": "string", "required": True,
             "path": "model.csvReaderParameters.multiFileSelectionParams.source.path.path",
             "description": "Absolute or workflow-relative path of the CSV file."},
            {"name": "first_row_header", "type": "boolean", "default": True,
             "path": "model.csvReaderParameters.firstRowContainsColumnNamesParams.firstRowContainsColumnNames",
             "description": "First row contains column names."},
            {"name": "delimiter", "type": "string", "default": ",",
             "path": "model.csvReaderParameters.csvFormatParams.columnDelimiter",
             "description": "Column delimiter (e.g. ',', ';', tab)."},
        ],
    },
    EXCEL_WRITER: {
        "id": "excel_writer",
        "description": "Write the input table to an Excel (.xlsx) file.",
        "inputs": ["table"],
        "outputs": [],
        "path_params": ["path"],
        "post_set": [
            {"path": "model.file_selection.path.file_system_type", "value": "LOCAL"},
            {"path": "model.file_selection.path.location_present", "value": True},
        ],
        "params": [
            {"name": "path", "type": "string", "required": True,
             "path": "model.file_selection.path.path",
             "description": "Destination .xlsx path (absolute or workflow-relative)."},
            {"name": "sheet", "type": "string", "default": "Sheet1",
             "path": "model.sheet_names.0",
             "description": "Worksheet name."},
            {"name": "if_exists", "type": "string", "default": "overwrite",
             "path": "model.file_selection.if_path_exists",
             "description": "overwrite | append -- what to do if the file exists."},
            {"name": "if_sheet_exists", "type": "string", "default": "OVERWRITE",
             "path": "model.if_sheet_exists",
             "description": "OVERWRITE | FAIL | APPEND -- if the sheet exists."},
            {"name": "write_column_header", "type": "boolean", "default": True,
             "path": "model.write_column_header",
             "description": "Write the column names as the first row."},
            {"name": "write_row_key", "type": "boolean", "default": False,
             "path": "model.write_row_key",
             "description": "Also write KNIME's RowID column."},
        ],
    },
    TABLE_WRITER: {
        "id": "table_writer",
        "description": "Write the input table to a KNIME .table file.",
        "inputs": ["table"],
        "outputs": [],
        "path_params": ["path"],
        "post_set": [
            {"path": "model.file_selection.path.file_system_type", "value": "LOCAL"},
            {"path": "model.file_selection.path.location_present", "value": True},
        ],
        "params": [
            {"name": "path", "type": "string", "required": True,
             "path": "model.file_selection.path.path",
             "description": "Destination .table path (absolute or workflow-relative)."},
        ],
    },
    COLUMN_FILTER: {
        "id": "column_filter",
        "description": "Keep only the listed columns (include) or drop the listed ones (exclude).",
        "inputs": ["table"],
        "outputs": ["table"],
        "params": [
            {"name": "include", "type": "list",
             "path": "model.column-filter.included_names",
             "description": "Columns to keep (mutually exclusive with exclude).",
             "clear_on_set": ["model.column-filter.excluded_names"]},
            {"name": "exclude", "type": "list",
             "path": "model.column-filter.excluded_names",
             "description": "Columns to drop (mutually exclusive with include).",
             "clear_on_set": ["model.column-filter.included_names"]},
            {"name": "pattern", "type": "string",
             "path": "model.column-filter.name_pattern.pattern",
             "description": "Wildcard/regex pattern (pattern mode)."},
            {"name": "pattern_type", "type": "string", "default": "Wildcard",
             "path": "model.column-filter.name_pattern.type",
             "description": "Wildcard | Regex."},
            {"name": "pattern_case_sensitive", "type": "boolean", "default": True,
             "path": "model.column-filter.name_pattern.caseSensitive",
             "description": "Case-sensitive pattern matching."},
            {"name": "pattern_exclude_matching", "type": "boolean", "default": False,
             "path": "model.column-filter.name_pattern.excludeMatching",
             "description": "True = drop matching columns instead of keeping them."},
        ],
    },
    ROW_FILTER: {
        "id": "row_filter",
        "description": "Keep rows matching a condition on one column.",
        "inputs": ["table"],
        "outputs": ["table"],
        "params": [
            {"name": "column", "type": "string", "required": True,
             "path": "model.predicates.0.columnV2.regularChoice",
             "description": "Column to test."},
            {"name": "operator", "type": "string", "default": "EQ",
             "path": "model.predicates.0.operator",
             "description": "EQ NEQ GT GE LT LE CONTAINS STARTS_WITH ENDS_WITH ..."},
            {"name": "value", "type": "string",
             "path": "model.predicates.0.filterValueParameters.value",
             "description": "Value to compare against."},
            {"name": "output_mode", "type": "string", "default": "MATCHING",
             "path": "model.outputMode",
             "description": "MATCHING keeps matching rows, NON_MATCHING inverts."},
            {"name": "match_criteria", "type": "string", "default": "AND",
             "path": "model.matchCriteria",
             "description": "AND | OR when several predicates exist."},
        ],
    },
    SORTER: {
        "id": "sorter",
        "description": "Sort rows by one or more columns.",
        "inputs": ["table"],
        "outputs": ["table"],
        "params": [
            {"name": "columns", "type": "list", "required": True,
             "path": "model.incllist",
             "description": "Columns to sort by, in order."},
            {"name": "descending", "type": "list", "sync": ["columns", False],
             "path": "model.sortOrder",
             "description": "true/false per column (false = ascending). Auto-fills "
                           "with false when omitted; must match columns length.",
             "clear_on_set": []},
            {"name": "alphanumeric", "type": "list", "sync": ["columns", False],
             "path": "model.alphaNumStringComp",
             "description": "true = alphanumeric (natural) string order, per column. "
                           "Auto-fills with false when omitted.",
             "clear_on_set": []},
            {"name": "missing_to_end", "type": "boolean", "default": False,
             "path": "model.missingToEnd",
             "description": "Sort missing values to the end."},
        ],
    },
    COLUMN_RENAMER: {
        "id": "column_renamer",
        "description": "Rename columns.",
        "inputs": ["table"],
        "outputs": ["table"],
        "params": [
            {"name": "renames", "type": "list",
             "path": "model.renamings",
             "description": "List of {\"oldName\": ..., \"newName\": ...} objects."},
        ],
    },
    GROUPBY: {
        "id": "groupby",
        "description": "Group rows and aggregate columns (GroupBy).",
        "inputs": ["table"],
        "outputs": ["table"],
        "params": [
            {"name": "group_by", "type": "list",
             "path": "model.grouByColumns.InclList",
             "description": "Columns to group by.",
             "clear_on_set": ["model.grouByColumns.ExclList"]},
            {"name": "group_by_exclude", "type": "list",
             "path": "model.grouByColumns.ExclList",
             "description": "Alternative: group by every column EXCEPT these.",
             "clear_on_set": ["model.grouByColumns.InclList"]},
            {"name": "aggregations", "type": "records",
             "path": "model.aggregationColumn",
             "records_map": {"column": "columnNames",
                             "method": "aggregationMethod",
                             "cell_class": {"array": "columnTypes", "wrap": "cell_class"},
                             "include_missing": "inclMissingVals"},
             "description": "List of {column, method, cell_class, include_missing} "
                           "objects, one aggregation per entry. method examples: "
                           "Count_V2.5.0, Sum_V2.5.2, Mean_V2.5.2, Min, Max, "
                           "StandardDeviation_V2.5.2, First, Last, Distinct. "
                           "cell_class like org.knime.core.data.def.DoubleCell.",
             "clear_on_set": []},
        ],
    },
}


def snake(name: str) -> str:
    out = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()
    return out or "node"


def scan(root: Path) -> dict[str, list[tuple[int, str, Path]]]:
    """factory -> [(size, node-name, path)] sorted smallest-first."""
    groups: dict[str, list[tuple[int, str, Path]]] = defaultdict(list)
    for path in root.rglob("settings.xml"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fac = _FACTORY_RE.search(text)
        if not fac:
            continue
        name = _NODENAME_RE.search(text)
        groups[fac.group(1)].append((path.stat().st_size, name.group(1) if name else "?", path))
    for items in groups.values():
        items.sort(key=lambda t: t[0])
    return groups


def candidate_paths(
    items: list[tuple[int, str, Path]], meta: dict
) -> list[tuple[int, str, Path]]:
    """Drop instances that can't host the curated params.

    The workspace holds two generations of the same node's settings (older
    example workflows ship a legacy layout that KNIME migrates on load), so
    a curated template only works on instances that really contain its
    declared paths.  Parse each candidate and look the paths up exactly,
    then take the smallest survivor.
    """
    required = [p["path"] for p in meta.get("params", [])]
    required += [p["path"] for p in meta.get("post_set", [])]
    required += meta.get("clear", [])
    if not required:
        return items
    usable = []
    for size, name, path in items:
        try:
            cfg = xc.load(path)
        except Exception:  # noqa: BLE001 - unreadable/foreign file, skip it
            continue
        if all(_exists(cfg, dotted) for dotted in required):
            usable.append((size, name, path))
    return usable


def _exists(cfg: xc.Config, dotted: str) -> bool:
    """True if ``dotted`` resolves to an entry or a config (arrays use indexes)."""
    node: xc.Config | None = cfg
    parts = dotted.split(".")
    for index, part in enumerate(parts):
        if node is None:
            return False
        last = index == len(parts) - 1
        child_entry = node.get_entry(part)
        if child_entry is not None:
            if last:
                return True
            continue
        child_config = node.get_config(part)
        if child_config is None:
            return False
        if last:
            return True
        node = child_config
    return node is not None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--only", nargs="*", help="template ids (curated) to harvest")
    parser.add_argument("--list", action="store_true", help="list candidates only")
    args = parser.parse_args()

    groups = scan(args.root)
    print(f"scanned {args.root}: {len(groups)} distinct node factories")

    if args.list:
        for factory, items in sorted(groups.items(), key=lambda kv: kv[1][0][1].lower()):
            meta = CURATED.get(factory, {})
            tid = meta.get("id") or snake(items[0][1])
            print(f"  {len(items):3d}x {tid:24s} smallest={items[0][0]:7d}B  {items[0][1][:40]}")
        return

    args.out.mkdir(parents=True, exist_ok=True)
    done, skipped, no_candidate = [], [], []
    for factory, items in sorted(groups.items()):
        meta = CURATED.get(factory, {})
        template_id = meta.get("id") or snake(items[0][1])
        if args.only and template_id not in args.only:
            continue
        pool = candidate_paths(items, meta)
        if not pool:
            no_candidate.append((template_id, len(items)))
            continue
        size, node_name, source = pool[0]
        template = harvest(
            source,
            template_id,
            node_name=node_name,
            description=meta.get("description", ""),
            inputs=meta.get("inputs"),
            outputs=meta.get("outputs"),
            params=meta.get("params"),
            path_params=meta.get("path_params"),
            post_set=meta.get("post_set"),
            clear=meta.get("clear"),
            source=str(source.relative_to(args.root)),
        )
        try:
            save_template(template, args.out)
            done.append((template_id, size, len(template.params)))
        except ValueError as exc:
            skipped.append((template_id, str(exc)))

    print(f"harvested {len(done)} templates into {args.out}")
    for tid, size, nparams in done:
        print(f"  {tid:26s} {size:7d}B  {nparams} params")
    for tid, reason in skipped:
        print(f"  SKIPPED {tid}: {reason}")
    for tid, count in no_candidate:
        print(f"  NO USABLE INSTANCE for curated paths: {tid} ({count} candidates)")


if __name__ == "__main__":
    main()
