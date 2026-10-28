"""Render every curated template and verify the produced settings."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knime_mcp import xmlconfig as xc  # noqa: E402
from knime_mcp.templates import default_registry  # noqa: E402

BASE = Path(r"C:/ws/wf1")

CASES: list[tuple[str, dict]] = [
    ("csv_reader", {"path": "data/in.csv"}),
    ("excel_writer", {"path": "out.xlsx", "sheet": "Results"}),
    ("table_writer", {"path": "out.table"}),
    ("column_filter", {"include": ["A", "B"]}),
    ("column_filter", {"exclude": ["C"]}),
    ("row_filter", {"column": "DELAY", "operator": "GT", "value": "10"}),
    ("sorter", {"columns": ["A", "B"], "descending": [False, True]}),
    ("sorter", {"columns": ["A", "B", "C"]}),  # sync fills sortOrder
    ("column_renamer", {"renames": [{"oldName": "A", "newName": "Alpha"}]}),
    (
        "groupby",
        {
            "group_by": ["CITY"],
            "aggregations": [
                {
                    "column": "DELAY",
                    "method": "Mean_V2.5.2",
                    "cell_class": "org.knime.core.data.def.DoubleCell",
                    "include_missing": False,
                },
                {
                    "column": "FLIGHT",
                    "method": "Count_V2.5.0",
                    "cell_class": "org.knime.core.data.def.LongCell",
                    "include_missing": True,
                },
            ],
        },
    ),
]


def get(cfg: xc.Config, dotted: str):
    return cfg.find(dotted)


def main() -> None:
    registry = default_registry()
    failures = 0
    for template_id, params in CASES:
        template = registry.get(template_id)
        try:
            text = template.render(params=params, base_dir=BASE)
            cfg = xc.loads(text)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {template_id} {params}: {type(exc).__name__}: {exc}")
            continue
        state = cfg.get("state")
        print(f"ok   {template_id:16s} state={state} params={params if len(str(params))<70 else str(params)[:67]+'...'}")
        if state != "CONFIGURED":
            failures += 1
            print("     ^ unexpected state")

        if template_id == "csv_reader":
            p = "model.csvReaderParameters.multiFileSelectionParams.source.path"
            assert get(cfg, p + ".path") == "data/in.csv" or Path(
                get(cfg, p + ".path") or ""
            ).is_absolute(), get(cfg, p + ".path")
            print("     path =", get(cfg, p + ".path"))
            print("     fstype =", get(cfg, p + ".file_system_type"))
            tp = cfg.get_config("model").get_config("transformationParameters")
            print("     transformationParameters children =", len(tp.children) if tp else None)
        elif template_id == "excel_writer":
            print("     path =", get(cfg, "model.file_selection.path.path"))
            print("     fstype =", get(cfg, "model.file_selection.path.file_system_type"))
            print("     sheet.0 =", get(cfg, "model.sheet_names.0"))
            print("     if_exists =", get(cfg, "model.if_path_exists"))
            print("     write_row_key =", get(cfg, "model.write_row_key"))
        elif template_id == "table_writer":
            print("     path =", get(cfg, "model.file_selection.path.path"))
            print("     fstype =", get(cfg, "model.file_selection.path.file_system_type"))
        elif template_id == "column_filter":
            print("     incl =", _array(cfg, "model.column-filter.included_names"))
            print("     excl =", _array(cfg, "model.column-filter.excluded_names"))
        elif template_id == "row_filter":
            print("     column =", get(cfg, "model.predicates.0.columnV2.regularChoice"))
            print("     operator =", get(cfg, "model.predicates.0.operator"))
            print("     value =", get(cfg, "model.predicates.0.filterValueParameters.value"))
        elif template_id == "sorter":
            print("     incllist =", _array(cfg, "model.incllist"))
            print("     sortOrder =", _array(cfg, "model.sortOrder"))
            print("     alphaNum =", _array(cfg, "model.alphaNumStringComp"))
        elif template_id == "column_renamer":
            print("     renamings size =", _array(cfg, "model.renamings", sized=False))
        elif template_id == "groupby":
            print("     InclList =", _array(cfg, "model.grouByColumns.InclList"))
            print("     ExclList size =", _array(cfg, "model.grouByColumns.ExclList"))
            print("     columnNames =", _array(cfg, "model.aggregationColumn.columnNames"))
            print("     methods =", _array(cfg, "model.aggregationColumn.aggregationMethod"))
            ct = cfg.get_config("model").get_config("aggregationColumn").get_config("columnTypes")
            if ct:
                for child in ct.children:
                    if isinstance(child, xc.Config):
                        print(
                            "     columnTypes",
                            child.key,
                            {k: v for k, v in [(c.key, c.python_value) for c in child.children]},
                        )
            print("     inclMissing =", _array(cfg, "model.aggregationColumn.inclMissingVals"))

    # required-param enforcement
    try:
        registry.get("csv_reader").render(params={}, base_dir=BASE)
    except ValueError as exc:
        print("ok   missing-required raises:", exc)
    else:
        failures += 1
        print("FAIL missing-required did not raise")

    print()
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)


def _array(cfg: xc.Config, dotted: str, sized: bool = True):
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.get_config(part) if node else None
        if node is None:
            return None
    child = node.get_config(parts[-1]) if node else None
    if child is None:
        return None
    out = []
    for c in child.children:
        if isinstance(c, xc.Entry) and c.key != "array-size":
            out.append(c.python_value)
        elif isinstance(c, xc.Config):
            out.append("<config>")
    return out


if __name__ == "__main__":
    main()
