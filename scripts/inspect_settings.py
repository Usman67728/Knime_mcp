"""Dump the settings paths of a node instance -- used to design templates.

    python scripts/inspect_settings.py csv_reader
    python scripts/inspect_settings.py --factory org.knime.base...
    python scripts/inspect_settings.py --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knime_mcp.templates import flat_entries  # noqa: E402

KNOWN = {
    "csv_reader": "org.knime.base.node.io.filehandling.csv.reader.CSVTableReaderNodeFactory",
    "excel_reader": "org.knime.ext.poi3.node.io.filehandling.excel.reader.ExcelTableReaderNodeFactory",
    "excel_writer": "org.knime.ext.poi3.node.io.filehandling.excel.writer.ExcelTableWriterNodeFactory",
    "table_writer": "org.knime.base.node.io.filehandling.table.writer.TableWriterNodeFactory",
    "table_creator": "org.knime.base.node.io.tablecreator.TableCreator3NodeFactory",
    "column_filter": "org.knime.base.node.preproc.filter.column.DataColumnSpecFilterNodeFactory",
    "row_filter": "org.knime.base.node.preproc.filter.row3.RowFilterNodeFactory",
    "sorter": "org.knime.base.node.preproc.sorter.SorterNodeFactory",
    "column_renamer": "org.knime.base.node.preproc.column.renamer.ColumnRenamerNodeFactory",
    "groupby": "org.knime.base.node.preproc.groupby.GroupByNodeFactory",
    "string_manipulation": "org.knime.base.node.preproc.stringmanipulation.StringManipulationNodeFactory",
    "math_formula": "org.knime.ext.jep.JEPNodeFactory",
    "rule_engine": "org.knime.base.node.rules.engine.RuleEngineNodeFactory",
    "constant_value_column": "org.knime.base.node.preproc.constantvalue.ConstantValueColumnNodeFactory",
    "missing_value": "org.knime.base.node.preproc.pmml.missingval.compute.MissingValueHandlerNodeFactory",
    "normalizer": "org.knime.base.node.preproc.normalize3.Normalizer3NodeFactory",
    "duplicate_row_filter": "org.knime.base.node.preproc.duplicates.DuplicateRowFilterNodeFactory",
    "concatenate": "org.knime.base.node.preproc.append.row.AppendedRowsNodeFactory",
    "number_to_string": "org.knime.base.node.preproc.colconvert.numbertostring2.NumberToString2NodeFactory",
    "string_to_number": "org.knime.base.node.preproc.colconvert.stringtonumber2.StringToNumber2NodeFactory",
    "rowid": "org.knime.base.node.preproc.rowkey2.RowKeyNodeFactory2",
    "table_cropper": "org.knime.base.node.preproc.table.cropper.TableCropperNodeFactory",
    "value_counter": "org.knime.base.node.preproc.valcount.ValueCounterNodeFactory",
    "statistics": "org.knime.base.node.stats.viz.extended.ExtendedStatisticsNodeFactory",
    "cell_updater": "org.knime.base.node.preproc.table.cellupdater.CellUpdaterNodeFactory",
    "column_appender": "org.knime.base.node.preproc.columnappend2.ColumnAppender2NodeFactory",
    "ungroup": "org.knime.base.node.preproc.ungroup.UngroupNodeFactory",
    "table_row_to_variable": "org.knime.base.node.flowvariable.tablerowtovariable3.TableToVariable3NodeFactory",
    "pmml_writer": "org.knime.base.node.io.filehandling.pmml.writer.PMMLWriterNodeFactory2",
}


def find_settings(root: Path, factory: str) -> Path | None:
    for path in root.rglob("settings.xml"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f'value="{factory}"' in text:
            return path
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", help="template names from KNOWN")
    parser.add_argument("--factory")
    parser.add_argument("--root", default=r"C:\Users\chusm\knime-workspace")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--internals", action="store_true", help="show *Internals* too")
    args = parser.parse_args()

    root = Path(args.root)
    jobs: list[tuple[str, str]] = []
    if args.factory:
        jobs.append((args.factory, args.factory))
    elif args.all or not args.names:
        jobs = list(KNOWN.items())
    else:
        jobs = [(name, KNOWN[name]) for name in args.names if name in KNOWN]

    for name, factory in jobs:
        hit = find_settings(root, factory)
        print("=" * 30, name)
        if not hit:
            print("  NOT FOUND in workspace")
            continue
        print("  source:", hit)
        for key, value in flat_entries(hit).items():
            if not key.startswith("model."):
                continue
            if not args.internals and "Internals" in key:
                continue
            print(f"  {key} = {value!r}")


if __name__ == "__main__":
    main()
