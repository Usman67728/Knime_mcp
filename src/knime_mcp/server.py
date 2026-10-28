"""KNIME MCP server -- expose a local KNIME Analytics Platform to agents.

Tool groups
-----------
*status*      what is installed, where the workspace is, what is broken
*browse*      find workflows, describe their graph, read their logs
*templates*   the node library used to build workflows, and how to extend it
*build*       create / validate / import / export / delete workflows
*run*         execute headlessly and collect the files that came out
*results*     preview CSV / TSV / JSON / Excel outputs
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import builder, executor, results, workspace
from .config import get_env, reset_env
from .templates import default_registry, flat_entries, harvest

mcp = FastMCP(
    "knime",
    instructions=(
        "Tools for a local KNIME Analytics Platform install: browse the workspace, "
        "build workflows from node templates, execute them headlessly, and read the "
        "files they produce. Start with knime_status, then list_templates before "
        "create_workflow."
    ),
)


# -- helpers ------------------------------------------------------------------

def _resolve(path: str, must_exist: bool = True) -> Path:
    """Relative paths are workspace-relative; expand ``~`` and env vars."""
    env = get_env()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = env.workspace / candidate
    candidate = candidate.resolve()
    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"path does not exist: {candidate}")
    return candidate


def _workflow_path(path: str) -> Path:
    resolved = _resolve(path)
    if not (resolved / "workflow.knime").exists():
        # maybe they passed the workspace-relative name of the workflow folder
        raise FileNotFoundError(
            f"{resolved} is not a KNIME workflow (no workflow.knime). "
            "Use list_workflows to find the right path."
        )
    return resolved


def _find_node(info: workspace.WorkflowInfo, node: str) -> workspace.NodeInfo:
    """Accept a node id, folder name, or display name."""
    stripped = node.strip()
    if stripped.isdigit():
        found = info.node(int(stripped))
        if found:
            return found
    for candidate in info.nodes:
        if candidate.folder == stripped or candidate.name == stripped:
            return candidate
    available = ", ".join(f"{n.folder}" for n in info.nodes) or "(none)"
    raise KeyError(f"no node '{node}' in {info.name}. Nodes: {available}")


def _registry():
    return default_registry()


# -- status -------------------------------------------------------------------

@mcp.tool()
def knime_status() -> dict:
    """Report the KNIME install, workspace, available executor and any blockers.

    Call this first: it tells you the exact workspace path to use and whether
    headless execution is possible at all.
    """
    env = get_env()
    status = env.describe()
    status["executor"] = executor.executor_hint(env)
    status["knime_version"] = builder.installed_build_id(env.knime_home)
    status["templates"] = len(_registry().ids())
    status["batch_workspace"] = str(
        env.batch_workspace or Path(str(env.workspace) + "-mcp-batch")
    )
    return status


@mcp.tool()
def reset_config() -> dict:
    """Forget the cached configuration (after editing .knime-mcp.json)."""
    reset_env()
    return knime_status()


# -- browse -------------------------------------------------------------------

@mcp.tool()
def list_workflows(folder: str = "", query: str = "") -> dict:
    """List workflows in the workspace (optionally under ``folder``).

    ``query`` filters by workflow name substring. Returns workspace-relative
    paths -- pass those to the other tools.
    """
    env = get_env()
    root = env.workspace / folder if folder else env.workspace
    if not root.exists():
        raise FileNotFoundError(f"folder does not exist: {root}")
    found = workspace.find_workflows(root)
    entries = []
    for path in found:
        if query and query.lower() not in path.name.lower():
            continue
        cfg_path = path / "workflow.knime"
        state = None
        try:
            state = workspace.xc.load(cfg_path).get("state")
        except Exception:  # noqa: BLE001
            state = "unreadable"
        try:
            relative = path.relative_to(env.workspace).as_posix()
        except ValueError:
            relative = str(path)
        entries.append(
            {"path": relative, "name": path.name, "state": state, "nodes": _count_nodes(path)}
        )
    return {"root": str(root), "count": len(entries), "workflows": entries}


def _count_nodes(path: Path) -> int:
    try:
        cfg = workspace.xc.load(path / "workflow.knime")
    except Exception:  # noqa: BLE001
        return -1
    nodes = cfg.get_config("nodes")
    return len(nodes.children) if nodes else 0


@mcp.tool()
def describe_workflow(path: str) -> dict:
    """Full picture of one workflow: nodes, factories, states, connections, problems."""
    return workspace.describe(_workflow_path(path)).to_dict()


@mcp.tool()
def get_workflow_log(path: str, max_chars: int = 8000) -> dict:
    """Tail of the workflow's ``knime.log`` (execution errors live here)."""
    workflow = _workflow_path(path)
    return {"path": str(workflow), "log": workspace.read_log(workflow)[-max_chars:]}


# -- templates ----------------------------------------------------------------

@mcp.tool()
def list_templates(query: str = "") -> dict:
    """Node templates available for building workflows (searchable).

    Each template is a pre-configured ``settings.xml`` skeleton with named
    ``params``. Use the returned ``id`` as the ``template`` value in
    create_workflow. An empty ``query`` lists everything.
    """
    registry = _registry()
    matches = registry.search(query)
    return {
        "count": len(matches),
        "templates": [t.to_dict() for t in matches],
        "hint": "get_template(id) documents a template's params and settings paths.",
    }


@mcp.tool()
def get_template(template_id: str) -> dict:
    """Everything needed to use one template: params, ports, factory, description."""
    template = _registry().get(template_id)
    data = template.to_dict()
    data["settings_paths"] = sorted(
        k for k in flat_entries_from_text(template.settings_text)
        if k.startswith("model.") or k.startswith("state")
    )
    return data


def flat_entries_from_text(text: str) -> dict[str, Any]:
    from . import xmlconfig as xc

    cfg = xc.loads(text)
    out: dict[str, Any] = {}

    def walk(node: xc.Config, prefix: str) -> None:
        for child in node.children:
            key = f"{prefix}.{child.key}" if prefix else child.key
            if isinstance(child, xc.Entry):
                out[key] = child.python_value
            else:
                walk(child, key)

    walk(cfg, "")
    return out


@mcp.tool()
def inspect_node_settings(path: str, node: str) -> dict:
    """Dump one node's settings as flat ``{"path.to.entry": value}``.

    This is how you discover which ``overrides`` a node accepts when its
    template has no named parameter for what you need to change.
    """
    workflow = _workflow_path(path)
    info = workspace.describe(workflow)
    target = _find_node(info, node)
    if not target.settings_file.exists():
        raise FileNotFoundError(f"{target.folder} has no settings.xml")
    return {
        "node": target.folder,
        "factory": target.factory,
        "state": target.state,
        "settings": flat_entries(target.settings_file),
    }


@mcp.tool()
def harvest_template(
    path: str,
    node: str,
    template_id: str,
    description: str = "",
    params: Optional[list[dict]] = None,
    inputs: Optional[list[str]] = None,
    outputs: Optional[list[str]] = None,
    overwrite: bool = False,
) -> dict:
    """Turn a configured node from an existing workflow into a reusable template.

    Use this whenever a node you need has no template yet: build it once in
    KNIME (or find a workflow that has it), then harvest it. ``params`` is a
    list of ``{"name": ..., "path": "model.some.entry", "type": "string",
    "required": false, "description": ...}`` entries.
    """
    registry = _registry()
    if template_id in registry and not overwrite:
        raise FileExistsError(
            f"template '{template_id}' already exists; pass overwrite=true to replace it"
        )
    workflow = _workflow_path(path)
    info = workspace.describe(workflow)
    target = _find_node(info, node)
    if not target.settings_file.exists():
        raise FileNotFoundError(f"{target.folder} has no settings.xml")

    template = harvest(
        target.settings_file,
        template_id,
        description=description,
        params=params or [],
        inputs=inputs,
        outputs=outputs,
        source=f"{workflow} :: {target.folder}",
    )
    # default template folder lives next to the project; allow env override
    import os

    directory = Path(os.environ.get("KNIME_MCP_TEMPLATE_DIR") or (Path.cwd() / "templates"))
    directory.mkdir(parents=True, exist_ok=True)
    saved = registry.save(template, directory)
    return {
        "id": template.id,
        "saved_to": str(saved),
        "factory": template.factory,
        "params": [p["name"] for p in template.to_dict()["params"]],
        "message": f"template '{template.id}' is now usable in create_workflow",
    }


# -- build --------------------------------------------------------------------

@mcp.tool()
def create_workflow(spec: dict) -> dict:
    """Create a KNIME workflow folder from a declarative spec.

    ``spec`` keys:
      name        workflow name (required, becomes the folder name)
      folder      workspace-relative parent folder ("" = workspace root)
      description text stored in the workflow metadata
      tags        list of strings
      overwrite   rebuild if the folder already exists (default false)
      nodes       list of {id, template, params?, overrides?, name?, annotation?, x?, y?}
      connections list of {from, to, from_port?, to_port?} using node ``id``s

    ``params`` are the template's named parameters; ``overrides`` set any
    settings entry directly by dotted path (see inspect_node_settings).
    Returns the built workflow's description plus any validation problems.
    """
    spec = builder.WorkflowSpec.from_dict(spec)
    env = get_env()
    registry = _registry()
    target = builder.build(spec, env, registry)
    info = workspace.describe(target)
    try:
        relative = target.relative_to(env.workspace).as_posix()
    except ValueError:
        relative = str(target)
    return {
        "created": relative,
        "absolute_path": str(target),
        "nodes": len(info.nodes),
        "connections": len(info.connections),
        "problems": info.problems,
        "next": [
            f"create_workflow returned a valid graph; call validate_workflow('{relative}') "
            "or execute_workflow('" + relative + "')",
        ],
    }


@mcp.tool()
def validate_workflow(path: str) -> dict:
    """Check a workflow without running it: structure, node states, missing extensions."""
    env = get_env()
    workflow = _workflow_path(path)
    info = workspace.describe(workflow)
    problems = workspace.validate(info, workspace.installed_bundles(env.knime_home))
    return {
        "path": str(workflow),
        "nodes": len(info.nodes),
        "connections": len(info.connections),
        "problems": problems,
        "ok": not [p for p in problems if p.get("severity") == "error"],
    }


@mcp.tool()
def delete_workflow(path: str, confirm: bool = False) -> dict:
    """Delete a workflow folder. Requires ``confirm=true`` -- this is permanent."""
    workflow = _workflow_path(path)
    if not confirm:
        return {
            "deleted": False,
            "path": str(workflow),
            "message": "Re-run with confirm=true to permanently delete this workflow.",
        }
    import shutil

    shutil.rmtree(workflow)
    return {"deleted": True, "path": str(workflow)}


# -- import / export ----------------------------------------------------------

@mcp.tool()
def export_workflow(path: str, dest: str) -> dict:
    """Zip a workflow folder into a ``.knwf`` archive (shareable / portable)."""
    workflow = _workflow_path(path)
    target = Path(dest).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.suffix.lower() != ".knwf":
        target = target.with_suffix(".knwf")
    out = workspace.export_knwf(workflow, target)
    return {"exported": str(out), "size": out.stat().st_size}


@mcp.tool()
def import_workflow(archive: str, folder: str = "", overwrite: bool = False) -> dict:
    """Unpack a ``.knwf`` archive into the workspace and return its path."""
    source = _resolve(archive)
    env = get_env()
    base = env.workspace / folder if folder else env.workspace
    base.mkdir(parents=True, exist_ok=True)
    dest = base / source.stem
    target = workspace.import_knwf(source, dest, overwrite=overwrite)
    info = workspace.describe(target)
    try:
        relative = target.relative_to(env.workspace).as_posix()
    except ValueError:
        relative = str(target)
    return {
        "imported": relative,
        "nodes": len(info.nodes),
        "problems": info.problems,
    }


# -- run ----------------------------------------------------------------------

@mcp.tool()
def execute_workflow(
    path: str,
    save: bool = True,
    reset: bool = True,
    variables: Optional[dict] = None,
    timeout: int = 0,
    output_dirs: Optional[list[str]] = None,
) -> dict:
    """Run a workflow headlessly and report status + files it produced.

    ``variables`` are KNIME workflow variables (``{"year": 2025}``), passed
    via ``-v`` to the executor. ``timeout`` in seconds (0 = configured
    default). ``output_dirs`` adds folders to scan for produced files
    (defaults to the workflow folder itself).
    """
    import time as _time

    workflow = _workflow_path(path)
    env = get_env()
    started = _time.time()
    result = executor.execute(
        workflow,
        env,
        save=save,
        reset=reset,
        variables=variables or {},
        timeout=timeout or None,
    )
    roots = [workflow]
    for extra in output_dirs or []:
        roots.append(_resolve(extra, must_exist=False))
    produced = results.find_outputs(roots, since=started)

    data = result.to_dict()
    data["produced_files"] = [f.to_dict() for f in produced]
    if not data["ok"] and not data["problems"]:
        data["problems"] = [
            {"severity": "error", "message": "execution reported failure without detail"}
        ]
    data["hint"] = (
        "read_table(path) previews a produced CSV/TSV/JSON/XLSX file."
        if produced
        else "No output files detected. Add a writer node (excel_writer / table_writer) "
        "or pass output_dirs."
    )
    return data


@mcp.tool()
def list_output_files(
    path: str = "",
    output_dirs: Optional[list[str]] = None,
    since: float = 0,
    limit: int = 100,
) -> dict:
    """List files a workflow produced (optionally only newer than ``since``).

    ``path`` is a workflow folder (its contents are scanned). Relative paths
    resolve against the workspace; ``output_dirs`` adds arbitrary folders.
    """
    roots: list[Path] = []
    if path:
        roots.append(_workflow_path(path))
    for extra in output_dirs or []:
        roots.append(_resolve(extra, must_exist=False))
    if not roots:
        roots.append(get_env().workspace)
    found = results.find_outputs(roots, since=since or None, limit=limit)
    return {"count": len(found), "files": [f.to_dict() for f in found]}


@mcp.tool()
def read_table(path: str, max_rows: int = 50, sheet: str = "") -> dict:
    """Preview a produced table file (CSV, TSV, JSON/JSONL, XLSX).

    Returns ``columns`` + ``rows`` + ``row_count``. For XLSX, ``sheet`` picks
    a worksheet by name (defaults to the first one).
    """
    target = _resolve(path)
    return results.read_table(target, max_rows=max_rows, sheet=sheet or None)


@mcp.tool()
def read_file(path: str, max_chars: int = 8000) -> dict:
    """Read a text output file (or the first bytes of a binary one)."""
    target = _resolve(path)
    info = results.describe_file(target)
    if "head" in info:
        info["head"] = info["head"][:max_chars]
    return info


# -- entry point --------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
