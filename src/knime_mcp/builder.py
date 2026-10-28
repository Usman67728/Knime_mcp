"""Assemble a KNIME workflow folder from a declarative spec.

This is the heart of the "create a workflow" capability: given a list of
node templates plus connections between them, it writes the exact file
layout KNIME expects -- ``workflow.knime``, one folder per node with its
``settings.xml``, plus the metadata files.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import xmlconfig as xc
from .config import KnimeEnv
from .templates import TemplateRegistry

# Files that should never be overwritten when a build targets an existing dir.
_NODE_DIR_RE = re.compile(r"^(?P<name>.+) \(\#(?P<id>\d+)\)$")

DEFAULT_EDITOR_SETTINGS = {
    "workflow.editor.snapToGrid": True,
    "workflow.editor.ShowGrid": True,
    "workflow.editor.gridX": 20,
    "workflow.editor.gridY": 20,
    "workflow.editor.zoomLevel": 1.0,
    "workflow.editor.curvedConnections": True,
    "workflow.editor.connectionWidth": 2,
}


@dataclass
class NodeSpec:
    """One node in the workflow, referenced by a template id."""

    ref: str  # caller-facing label used by connections
    template: str
    params: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    name: Optional[str] = None  # display name; defaults to the template's
    annotation: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> "NodeSpec":
        if "template" not in data:
            raise ValueError(f"node {data.get('id') or data.get('ref')!r} has no 'template'")
        ref = str(data.get("id") or data.get("ref") or data["template"])
        return cls(
            ref=ref,
            template=data["template"],
            params=dict(data.get("params", {})),
            overrides=dict(data.get("overrides", {})),
            name=data.get("name"),
            annotation=data.get("annotation"),
            x=data.get("x"),
            y=data.get("y"),
        )


@dataclass
class ConnectionSpec:
    source: str
    dest: str
    source_port: int = 1
    dest_port: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionSpec":
        try:
            source = data.get("from", data.get("source"))
            dest = data.get("to", data.get("dest"))
        except AttributeError:  # pragma: no cover - list passed by mistake
            raise ValueError("connection must be an object") from None
        if source is None or dest is None:
            raise ValueError(f"connection needs 'from' and 'to': {data}")
        return cls(
            source=str(source),
            dest=str(dest),
            source_port=int(data.get("from_port", data.get("source_port", 1))),
            dest_port=int(data.get("to_port", data.get("dest_port", 1))),
        )


@dataclass
class WorkflowSpec:
    name: str
    nodes: list[NodeSpec]
    connections: list[ConnectionSpec] = field(default_factory=list)
    folder: str = ""  # workspace-relative parent folder, "" = workspace root
    description: str = ""
    tags: list[str] = field(default_factory=list)
    overwrite: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowSpec":
        if not data.get("name"):
            raise ValueError("'name' is required")
        raw_nodes = data.get("nodes") or []
        if not raw_nodes:
            raise ValueError("'nodes' must contain at least one node")
        return cls(
            name=str(data["name"]),
            nodes=[NodeSpec.from_dict(n) for n in raw_nodes],
            connections=[ConnectionSpec.from_dict(c) for c in (data.get("connections") or [])],
            folder=str(data.get("folder", "")),
            description=str(data.get("description", "")),
            tags=[str(t) for t in (data.get("tags") or [])],
            overwrite=bool(data.get("overwrite", False)),
        )


# -- helpers ------------------------------------------------------------------

def installed_build_id(knime_home: Path) -> str:
    """e.g. ``5.12.0.v202606180846`` -- read from the core plug-in name.

    KNIME ships some bundles unpacked (a folder rather than a ``.jar``), so
    both layouts are accepted.
    """
    plugins = Path(knime_home) / "plugins"
    if plugins.exists():
        for entry in plugins.iterdir():
            # sub-bundles like org.knime.core.pmml_... never match this pattern
            m = re.match(r"^org\.knime\.core_(.+?)(?:\.jar)?$", entry.name)
            if m:
                return m.group(1)
    return "5.1.0.v000000000000"


def _safe_name(name: str) -> str:
    """Folder name KNIME tolerates (it disallows path separators etc.)."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "-", name).strip()
    return cleaned or "workflow"


def _auto_layout(nodes: list[NodeSpec], connections: list[ConnectionSpec]) -> None:
    """Place nodes left-to-right by dependency depth when not positioned."""
    if any(n.x is not None and n.y is not None for n in nodes):
        for i, node in enumerate(nodes):
            if node.x is None:
                node.x = 60 + i * 220
            if node.y is None:
                node.y = 60
        return

    depth: dict[str, int] = {n.ref: 0 for n in nodes}
    incoming: dict[str, list[str]] = {n.ref: [] for n in nodes}
    for conn in connections:
        if conn.dest in incoming:
            incoming[conn.dest].append(conn.source)

    for _ in range(len(nodes) + 1):  # Bellman-Ford-ish; tolerates accidental cycles
        changed = False
        for ref, sources in incoming.items():
            if not sources:
                continue
            candidate = max((depth.get(s, 0) + 1) for s in sources)
            if candidate > depth.get(ref, 0):
                depth[ref] = candidate
                changed = True
        if not changed:
            break

    rows: dict[int, int] = {}
    for node in sorted(nodes, key=lambda n: (depth.get(n.ref, 0), n.ref)):
        col = depth.get(node.ref, 0)
        row = rows.get(col, 0)
        rows[col] = row + 1
        node.x = 60 + col * 230
        node.y = 60 + row * 170


# -- workflow.knime ------------------------------------------------------------

def build_workflow_config(
    nodes: list[tuple[NodeSpec, int, str]],
    connections: list[tuple[ConnectionSpec, int, int]],
    *,
    created_by: str,
    name: str = "",
) -> xc.Config:
    """``nodes``: (spec, numeric id, folder); ``connections``: (spec, src, dst)."""
    root = xc.Config(key="workflow.knime")
    root.set("created_by", created_by)
    root.set("created_by_nightly", False)
    root.set("version", "5.1.0")
    root.set("name", None)
    root.config("authorInformation")
    root.set("customDescription", None)
    root.set("state", "IDLE")
    root.config("workflow_credentials")

    nodes_cfg = root.config("nodes")
    for spec, node_id, folder in nodes:
        entry = xc.Config(key=f"node_{node_id}")
        entry.set("id", node_id, type="xint")
        entry.set("node_settings_file", f"{folder}/settings.xml")
        entry.set("node_is_meta", False)
        entry.set("node_type", "NativeNode")
        entry.set("ui_classname", "org.knime.core.node.workflow.NodeUIInformation")
        ui = entry.config("ui_settings")
        bounds = ui.config("extrainfo.node.bounds")
        bounds.set("array-size", 4, type="xint")
        bounds.set("0", int(spec.x or 0), type="xint")
        bounds.set("1", int(spec.y or 0), type="xint")
        bounds.set("2", 74, type="xint")
        bounds.set("3", 78, type="xint")
        nodes_cfg.children.append(entry)

    conns_cfg = root.config("connections")
    for index, (conn, source_id, dest_id) in enumerate(connections):
        entry = xc.Config(key=f"connection_{index}")
        entry.set("sourceID", source_id, type="xint")
        entry.set("destID", dest_id, type="xint")
        entry.set("sourcePort", conn.source_port, type="xint")
        entry.set("destPort", conn.dest_port, type="xint")
        entry.set("ui_classname", "org.knime.core.node.workflow.ConnectionUIInformation")
        ui = entry.config("ui_settings")
        ui.set("extrainfo.conn.bendpoints_size", 0, type="xint")
        conns_cfg.children.append(entry)

    editor = root.config("workflow_editor_settings")
    for key, value in DEFAULT_EDITOR_SETTINGS.items():
        editor.set(key, value)

    return root


# -- metadata files -----------------------------------------------------------

def _workflow_metadata(description: str, tags: Iterable[str]) -> str:
    now = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    tag_xml = "\n".join(f"        <tag>{html.escape(t)}</tag>" for t in tags)
    tags_block = f"\n    <tags>\n{tag_xml}\n    </tags>" if tag_xml else ""
    description_block = (
        f"\n    <description>{html.escape(description)}</description>" if description else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<workflow-metadata content-type="text/plain" '
        'xmlns="http://www.knime.org/core/node/workflow/metadata/v1.0">\n'
        f"    <last-modified>{now}</last-modified>"
        f"{description_block}{tags_block}\n"
        "</workflow-metadata>\n"
    )


def _workflowset_meta(description: str, tags: Iterable[str]) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S +0000")
    comment = description or ""
    tag_list = list(tags)
    if tag_list:
        comment = (comment + "\n\nTAGS: " + ",".join(tag_list)).strip("\n")
    comment = comment.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!--This file is auto-generated and is overwritten each time the workflow is saved.\n"
        "    The metadata have moved to the `workflow-metadata.xml` file.-->\n"
        '<KNIMEMetaInfo nrOfElements="2" xmlns="http://www.knime.org/2.9/metainfo">\n'
        f'    <element form="multiline" name="Comments" read-only="false">{comment}</element>\n'
        f'    <element form="text" name="Last Edited" read-only="true">{now}</element>\n'
        "</KNIMEMetaInfo>\n"
    )


# -- build --------------------------------------------------------------------

def resolve_target(env: KnimeEnv, folder: str, name: str) -> Path:
    base = env.workspace
    if folder:
        base = base / folder
    return base / _safe_name(name)


def build(spec: WorkflowSpec, env: KnimeEnv, registry: TemplateRegistry) -> Path:
    """Create the workflow on disk and return its folder."""
    target = resolve_target(env, spec.folder, spec.name)
    if target.exists():
        if not spec.overwrite:
            raise FileExistsError(
                f"{target} already exists; set 'overwrite': true to rebuild it"
            )
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    # resolve templates up front so we fail before writing anything
    prepared: list[tuple[NodeSpec, int, str, str]] = []
    refs: dict[str, int] = {}
    for index, node_spec in enumerate(spec.nodes, start=1):
        template = registry.get(node_spec.template)
        folder = _safe_name(f"{node_spec.name or template.node_name} (#{index})")
        settings_text = template.render(
            params=node_spec.params,
            overrides=node_spec.overrides,
            node_name=node_spec.name or template.node_name,
            base_dir=target,
        )
        prepared.append((node_spec, index, folder, settings_text))
        if node_spec.ref in refs:
            raise ValueError(f"duplicate node id '{node_spec.ref}'")
        refs[node_spec.ref] = index

    missing = [
        c.source if c.source not in refs else c.dest
        for c in spec.connections
        if c.source not in refs or c.dest not in refs
    ]
    if missing:
        raise ValueError(
            f"connection references unknown node id(s): {', '.join(sorted(set(missing)))}. "
            f"Known ids: {', '.join(sorted(refs))}"
        )

    _auto_layout([p[0] for p in prepared], spec.connections)

    # write node folders
    for node_spec, _index, folder, settings_text in prepared:
        node_dir = target / folder
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "settings.xml").write_text(settings_text, encoding="utf-8")
        if node_spec.annotation:
            _write_annotation(node_dir, node_spec.annotation)

    connections = [
        (conn, refs[conn.source], refs[conn.dest]) for conn in spec.connections
    ]
    config = build_workflow_config(
        [(n, i, f) for n, i, f, _ in prepared],
        connections,
        created_by=installed_build_id(env.knime_home),
    )
    (target / "workflow.knime").write_text(xc.dumps(config), encoding="utf-8")
    (target / "workflow-metadata.xml").write_text(
        _workflow_metadata(spec.description, spec.tags), encoding="utf-8"
    )
    (target / "workflowset.meta").write_text(
        _workflowset_meta(spec.description, spec.tags), encoding="utf-8"
    )
    return target


def _write_annotation(node_dir: Path, text: str) -> None:
    """Store a node annotation inside the node's settings file."""
    settings = node_dir / "settings.xml"
    cfg = xc.load(settings)
    annotation = cfg.config("nodeAnnotation")
    annotation.children.clear()
    annotation.set("text", text)
    annotation.set("contentType", "text/plain")
    annotation.set("bgcolor", 16777215, type="xint")
    annotation.set("x-coordinate", 0, type="xint")
    annotation.set("y-coordinate", 100, type="xint")
    annotation.set("width", 160, type="xint")
    annotation.set("height", 60, type="xint")
    annotation.set("alignment", "CENTER")
    annotation.set("borderSize", 0, type="xint")
    annotation.set("borderColor", 16777215, type="xint")
    annotation.set("defFontSize", 9, type="xint")
    annotation.set("annotation-version", 20151123, type="xint")
    annotation.config("styles")
    xc.save(cfg, settings)


def spec_from_json(text: str) -> WorkflowSpec:
    return WorkflowSpec.from_dict(json.loads(text))
