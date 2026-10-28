# knime-mcp

An MCP server that drives a **local KNIME Analytics Platform** install: browse the
workspace, build workflows from node templates, execute them headlessly, and read
the files they produce.

```
agent ──MCP──> knime-mcp ──files──> <workspace>/*.knicw folders
                     └──subprocess──> knime.exe -application …NodePitBatchExecutor
```

Nothing runs inside KNIME's JVM except the workflow itself: workflows are plain
folders of KNIME `XMLConfig` files that this package writes directly, which makes
`create_workflow` / `describe_workflow` instant and independent of KNIME.

## Requirements

| Thing | Notes |
| --- | --- |
| Python ≥ 3.10 | with `pip` |
| KNIME Analytics Platform 5.x | autodetected, or set `KNIME_MCP_HOME` |
| A headless executor | see below |

### Headless executor

KNIME **discontinued its own Batch Executor for 5.9+ in April 2026** and pulled the
update site, so it cannot be installed any more. The MCP therefore uses
[NodePit Batch](https://nodepit.com/product/batch), the community drop-in
replacement (v5.12 supported):

```powershell
knime.exe -nosplash -consoleLog -application org.eclipse.equinox.p2.director `
  -repository https://download.nodepit.com/5.12 `
  -installIUs com.nodepit.batch.feature.feature.group -d "<knime-home>"
```

`knime_status()` reports whether an executor is present and prints the exact
command if it is missing. KNIME's classic `KNIME_BATCH_APPLICATION` is still
supported automatically for installations that already have it (≤ 5.8).

> Close the KNIME GUI before installing an extension. Executing workflows does
> *not* require closing it: headless runs use a separate `-data` workspace
> (`<workspace>-mcp-batch`) so they never lock horns with the GUI.

## Install

```powershell
cd knime-mcp
python -m pip install -e .
python -c "from knime_mcp.server import mcp; print('ok')"
```

Register it with an MCP client, e.g. for Claude Desktop / OpenCode:

```json
{
  "mcpServers": {
    "knime": {
      "command": "python",
      "args": ["-m", "knime_mcp.server"],
      "cwd": "C:\\path\\to\\knime-mcp"
    }
  }
}
```

`knime-mcp` is also installed as a console script.

## Configuration

Resolved in this order: environment variable → `.knime-mcp.json` (cwd or any
parent) → autodetection.

| Variable | Meaning |
| --- | --- |
| `KNIME_MCP_HOME` | folder containing `knime.exe` |
| `KNIME_MCP_WORKSPACE` | KNIME workspace (default: whatever KNIME last opened) |
| `KNIME_MCP_BATCH_WORKSPACE` | `-data` folder for headless runs |
| `KNIME_MCP_TIMEOUT` | per-execution timeout in seconds (default 1800) |
| `KNIME_MCP_TEMPLATE_DIR` | extra folder of node templates |

```jsonc
// .knime-mcp.json
{
  "workspace": "C:\\Users\\me\\knime-workspace",
  "execution_timeout": 900,
  "template_dirs": ["C:\\path\\to\\my-templates"],
  "extra_args": []
}
```

## Tools

**Status** — `knime_status`, `reset_config`
**Browse** — `list_workflows`, `describe_workflow`, `get_workflow_log`
**Templates** — `list_templates`, `get_template`, `inspect_node_settings`, `harvest_template`
**Build** — `create_workflow`, `validate_workflow`, `delete_workflow`, `export_workflow`, `import_workflow`
**Run** — `execute_workflow`, `list_output_files`
**Results** — `read_table` (CSV/TSV/JSON/XLSX), `read_file`

### Typical end-to-end flow

1. `knime_status()` — confirm workspace + executor.
2. `list_templates()` — see what nodes are available.
3. `create_workflow({name, nodes, connections})`.
4. `execute_workflow(path)` — returns exit code, node states, log tail and the
   files the run produced.
5. `read_table("<workflow>/out.xlsx")` — inspect the result.

### Building a workflow

```jsonc
{
  "name": "sales_summary",
  "folder": "MCP",
  "description": "Filter + aggregate sales",
  "nodes": [
    { "id": "src", "template": "csv_reader", "params": { "path": "../_io/sales.csv" } },
    { "id": "flt", "template": "row_filter",  "params": { "column": "region", "pattern": "EU*" } },
    { "id": "agg", "template": "groupby",     "overrides": { "model.settings.groupColumns": "…" } },
    { "id": "out", "template": "excel_writer", "params": { "path": "summary.xlsx" } }
  ],
  "connections": [
    { "from": "src", "to": "flt" },
    { "from": "flt", "to": "agg" },
    { "from": "agg", "to": "out" }
  ]
}
```

`params` are the template's named parameters. Anything else can be set with
`overrides`, a map of dotted settings paths → values; `inspect_node_settings`
dumps every path a node accepts.

## Node templates

A template is a harvested, working `settings.xml` plus metadata:

```
templates/csv_reader/
    settings.xml     # captured from a real, configured node
    template.json    # id, factory, ports, params, description
```

This avoids guessing the dozens of settings keys each node factory expects —
the baseline always comes from a node KNIME itself accepted.

Harvesting more is a first-class tool:

```jsonc
harvest_template({
  "path": "MyWorkflows/probe",
  "node": "CSV Writer (#7)",
  "template_id": "csv_writer",
  "description": "Writes a delimited text file",
  "outputs": ["file"],
  "params": [{ "name": "path", "path": "model.settings.path.path", "type": "string" }]
})
```

i.e. drop the node into any KNIME workflow once, configure it, save — then
harvest it and never think about it again.

## How it works

| Module | Responsibility |
| --- | --- |
| `xmlconfig.py` | read/write KNIME's `XMLConfig` (`<entry>`/`<config>`) byte-compatibly |
| `config.py` | find `knime.exe`, the workspace, executor capability checks |
| `workspace.py` | parse `workflow.knime` + per-node `settings.xml`, validate, `.knwf` zip |
| `templates.py` | template registry, parameter substitution, harvesting |
| `builder.py` | write a complete workflow folder from a declarative spec |
| `executor.py` | spawn `knime.exe` headlessly (NodePit or classic), parse results |
| `results.py` | find produced files, preview CSV/JSON/XLSX without pandas |

## Limitations / roadmap

* Templates only cover nodes harvested so far — `list_templates` shows the
  current library; extend it with `harvest_template`.
* KNIME's native `.knime` table format is not readable directly; write results
  with an Excel/CSV/JSON writer node.
* Execution needs an executor extension, which KNIME no longer ships itself.
