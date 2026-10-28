"""End-to-end test: build a workflow from templates, run it headlessly, read results.

Creates ``e2e_test.csv`` in the project, builds

    CSV Reader -> Column Filter -> Sorter -> Excel Writer
                -> Table Writer

executes it with the installed batch executor, then previews the produced
files.  This is the proof that the whole pipeline (templates -> builder ->
workflow.knime -> KNIME -> outputs) actually works.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knime_mcp import builder, executor, results, workspace  # noqa: E402
from knime_mcp.config import get_env  # noqa: E402
from knime_mcp.templates import default_registry  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
CSV = PROJECT / "e2e_test.csv"
WF_NAME = "mcp-e2e-test"

CSV_CONTENT = """id,city,delay,airline
1,Berlin,12,LH
2,Paris,-3,AF
3,Rome,45,BA
4,Berlin,7,LH
5,Paris,22,AF
6,Madrid,0,IB
7,Rome,-8,BA
8,Berlin,31,LH
"""


def main() -> None:
    CSV.write_text(CSV_CONTENT, encoding="utf-8")
    env = get_env()
    registry = default_registry()
    print("workspace :", env.workspace)
    print("executor  :", env.executor_kind)
    print("templates :", len(registry.ids()))
    print()

    spec = builder.WorkflowSpec.from_dict(
        {
            "name": WF_NAME,
            "description": "knime-mcp end-to-end test",
            "tags": ["mcp", "e2e"],
            "overwrite": True,
            "nodes": [
                {
                    "id": "read",
                    "template": "csv_reader",
                    "name": "Read CSV",
                    "params": {"path": str(CSV), "first_row_header": True, "delimiter": ","},
                },
                {
                    "id": "filter",
                    "template": "column_filter",
                    "name": "Keep cols",
                    "params": {"include": ["id", "city", "delay", "airline"]},
                },
                {
                    "id": "sort",
                    "template": "sorter",
                    "name": "Sort by delay",
                    "params": {"columns": ["delay"], "descending": [True]},
                },
                {
                    "id": "xl",
                    "template": "excel_writer",
                    "name": "Write Excel",
                    "params": {"path": "e2e_out.xlsx", "sheet": "Results"},
                },
                {
                    "id": "tbl",
                    "template": "table_writer",
                    "name": "Write Table",
                    "params": {"path": "e2e_out.table"},
                },
            ],
            "connections": [
                {"from": "read", "to": "filter"},
                {"from": "filter", "to": "sort"},
                {"from": "sort", "to": "xl"},
                {"from": "sort", "to": "tbl"},
            ],
        }
    )

    target = builder.build(spec, env, registry)
    print("built     :", target)
    info = workspace.describe(target)
    for node in info.nodes:
        print(f"   node {node.id:>2} {node.folder:24s} state={node.state}")
    print("problems  :", info.problems or "none")

    problems = workspace.validate(info, workspace.installed_bundles(env.knime_home))
    errors = [p for p in problems if p.get("severity") == "error"]
    print("validate  :", "OK" if not errors else errors)
    if errors:
        sys.exit(1)

    print()
    print("executing headlessly (this spawns the JVM, ~30-90s)...")
    started = time.time()
    result = executor.execute(target, env, save=True, reset=True, timeout=600)
    print(f"done in {time.time() - started:.1f}s")
    print("ok        :", result.ok)
    print("exit code :", result.exit_code)
    print("executor  :", result.executor)
    print("command   :", " ".join(result.command))
    for node in result.node_states:
        print(f"   node {node['id']:>2} {node['folder']:24s} state={node['state']}")
    if result.problems:
        print("problems  :")
        for p in result.problems:
            print("   -", p)
    if not result.ok:
        print()
        print("---- knime.log tail ----")
        print(result.log[-4000:])
        print("---- stdout tail ----")
        print(result.stdout[-2000:])
        sys.exit(2)

    print()
    produced = results.find_outputs([target])
    print("produced files:")
    for item in produced:
        print("   ", item.path, item.size, "bytes")
    if not produced:
        print("   (none!)")
        sys.exit(3)

    xlsx = [p for p in produced if p.path.suffix.lower() in (".xlsx", ".xls")]
    if xlsx:
        table = results.read_table(xlsx[0])
        print()
        print("excel columns:", table.get("columns"))
        print("excel rows   :", table.get("row_count"))
        for row in table.get("rows", [])[:10]:
            print("    ", row)
    print()
    print("E2E OK")


if __name__ == "__main__":
    main()
