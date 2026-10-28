"""Reading, describing and validating KNIME workflows on the local filesystem.

A KNIME workflow is a folder containing::

    my_workflow/
        workflow.knime          # graph: nodes + connections + editor state
        workflow-metadata.xml   # description / tags (optional)
        workflowset.meta        # repository metadata (optional, auto-generated)
        knime.log               # present after an execution
        CSV Reader (#14)/
            settings.xml        # node config: factory, model settings, state
        Sorter (#3)/
            settings.xml

Nothing in here talks to KNIME itself -- it is pure file parsing, so it is
fast enough to run on every MCP call and never needs a running KNIME.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import xmlconfig as xc

#: Files/folders that are never part of the repository tree.
_IGNORED_DIRS = {".metadata", ".artifacts", ".savedWithData", "internal", "dialog"}

_NODE_DIR_RE = re.compile(r"^(?P<name>.+) \(\#(?P<id>\d+)\)$")


# -- data model ---------------------------------------------------------------

@dataclass
class NodeInfo:
    id: int
    folder: str
    name: str
    settings_file: Path
    factory: str | None = None
    state: str | None = None
    bundle: str | None = None
    ports: list[int] = field(default_factory=list)
    bounds: tuple[int, int, int, int] | None = None
    error: str | None = None

    @property
    def configured(self) -> bool:
        return self.state in ("CONFIGURED", "EXECUTED")


@dataclass
class ConnectionInfo:
    source_id: int
    dest_id: int
    source_port: int
    dest_port: int


@dataclass
class WorkflowInfo:
    path: Path
    name: str
    nodes: list[NodeInfo] = field(default_factory=list)
    connections: list[ConnectionInfo] = field(default_factory=list)
    state: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)

    def node(self, node_id: int) -> NodeInfo | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "state": self.state,
            "description": self.description,
            "tags": self.tags,
            "node_count": len(self.nodes),
            "connection_count": len(self.connections),
            "nodes": [
                {
                    "id": n.id,
                    "name": n.name,
                    "folder": n.folder,
                    "factory": n.factory,
                    "state": n.state,
                    "bundle": n.bundle,
                    "configured": n.configured,
                    "error": n.error,
                }
                for n in self.nodes
            ],
            "connections": [
                {
                    "from": c.source_id,
                    "from_node": self.node(c.source_id).name if self.node(c.source_id) else None,
                    "from_port": c.source_port,
                    "to": c.dest_id,
                    "to_node": self.node(c.dest_id).name if self.node(c.dest_id) else None,
                    "to_port": c.dest_port,
                }
                for c in self.connections
            ],
            "problems": self.problems,
        }


# -- discovery ----------------------------------------------------------------

def is_workflow_dir(path: Path) -> bool:
    return (path / "workflow.knime").is_file()


def find_workflows(root: Path, max_depth: int = 6) -> list[Path]:
    """All workflow folders under ``root`` (workspace-relative discovery)."""
    found: list[Path] = []
    root = Path(root)

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            return
        if is_workflow_dir(directory):
            found.append(directory)
            return  # a workflow's node folders never contain nested workflows we want
        for child in children:
            if not child.is_dir() or child.name in _IGNORED_DIRS or child.name.startswith("."):
                continue
            walk(child, depth + 1)

    walk(root, 0)
    return found


# -- parsing ------------------------------------------------------------------

def _node_bounds(node_cfg: xc.Config) -> tuple[int, int, int, int] | None:
    ui = node_cfg.get_config("ui_settings")
    if ui is None:
        return None
    bounds = ui.get_config("extrainfo.node.bounds")
    if bounds is None:
        return None
    return (
        int(bounds.get("0", 0) or 0),
        int(bounds.get("1", 0) or 0),
        int(bounds.get("2", 74) or 74),
        int(bounds.get("3", 78) or 78),
    )


def _read_node_settings(settings_path: Path, folder_name: str, node_id: int) -> NodeInfo:
    info = NodeInfo(
        id=node_id,
        folder=folder_name,
        name=_NODE_DIR_RE.match(folder_name).group("name")
        if _NODE_DIR_RE.match(folder_name)
        else folder_name,
        settings_file=settings_path,
    )
    if not settings_path.exists():
        info.error = "settings.xml is missing"
        return info
    try:
        cfg = xc.load(settings_path)
    except Exception as exc:  # noqa: BLE001 - report, never crash the tool
        info.error = f"unreadable settings.xml: {exc}"
        return info
    info.factory = cfg.get("factory")
    info.state = cfg.get("state")
    info.bundle = cfg.get("node-bundle-symbolic-name")
    ports = cfg.get_config("ports")
    if ports is not None:
        info.ports = [
            int(child.get("index", 0) or 0)
            for child in ports.children
            if isinstance(child, xc.Config)
        ]
    info.bounds = _node_bounds(cfg)
    return info


def describe(path: Path) -> WorkflowInfo:
    """Parse a workflow folder into a :class:`WorkflowInfo`."""
    path = Path(path)
    info = WorkflowInfo(path=path, name=path.name)
    wf_file = path / "workflow.knime"
    if not wf_file.exists():
        info.problems.append(
            {"severity": "error", "message": "workflow.knime is missing -- not a KNIME workflow"}
        )
        return info

    try:
        cfg = xc.load(wf_file)
    except Exception as exc:  # noqa: BLE001
        info.problems.append({"severity": "error", "message": f"cannot parse workflow.knime: {exc}"})
        return info

    info.state = cfg.get("state")

    nodes_cfg = cfg.get_config("nodes")
    if nodes_cfg is not None:
        for child in nodes_cfg.children:
            if not isinstance(child, xc.Config) or not child.key.startswith("node_"):
                continue
            node_id = int(child.get("id", 0) or 0)
            settings_rel = child.get("node_settings_file", "") or ""
            folder = Path(settings_rel).parent.name if settings_rel else f"Node (#{node_id})"
            node_info = _read_node_settings(path / settings_rel, folder, node_id)
            node_info.bounds = _node_bounds(child) or node_info.bounds
            info.nodes.append(node_info)

    conns_cfg = cfg.get_config("connections")
    if conns_cfg is not None:
        for child in conns_cfg.children:
            if not isinstance(child, xc.Config):
                continue
            info.connections.append(
                ConnectionInfo(
                    source_id=int(child.get("sourceID", 0) or 0),
                    dest_id=int(child.get("destID", 0) or 0),
                    source_port=int(child.get("sourcePort", 1) or 1),
                    dest_port=int(child.get("destPort", 1) or 1),
                )
            )

    _read_metadata(path, info)
    info.problems = validate(info)
    return info


def _read_metadata(path: Path, info: WorkflowInfo) -> None:
    meta = path / "workflow-metadata.xml"
    if not meta.exists():
        return
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(meta.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "description" and child.text:
            info.description = child.text.strip()
        elif tag == "tags":
            info.tags = [t.text.strip() for t in child if t.text and t.text.strip()]


# -- validation ---------------------------------------------------------------

def validate(info: WorkflowInfo, installed_bundles: Optional[set[str]] = None) -> list[dict]:
    """Structural checks that predict whether a workflow will load & run.

    ``installed_bundles`` is the set of symbolic names of installed KNIME
    plug-ins; when given, nodes coming from uninstalled extensions are
    flagged as errors (this is the classic "node is missing" failure).
    """
    problems: list[dict] = []
    if not info.nodes:
        problems.append({"severity": "warning", "message": "workflow has no nodes"})

    ids = {n.id for n in info.nodes}
    for node in info.nodes:
        if node.error:
            problems.append(
                {"severity": "error", "node": node.folder, "message": node.error}
            )
        if not node.factory:
            problems.append(
                {
                    "severity": "error",
                    "node": node.folder,
                    "message": "node has no factory -- KNIME will show it as missing",
                }
            )
        if node.state == "IDLE":
            problems.append(
                {
                    "severity": "error",
                    "node": node.folder,
                    "message": "node is not configured (state IDLE); headless execution will skip/fail it",
                }
            )
        elif node.state not in ("CONFIGURED", "EXECUTED", "IDLE"):
            if node.state is not None:
                problems.append(
                    {
                        "severity": "warning",
                        "node": node.folder,
                        "message": f"unexpected node state {node.state!r}",
                    }
                )
        if installed_bundles and node.bundle and node.bundle not in installed_bundles:
            problems.append(
                {
                    "severity": "error",
                    "node": node.folder,
                    "message": f"extension '{node.bundle}' is not installed in this KNIME",
                }
            )

    # connections
    node_ids = {n.id for n in info.nodes}
    seen: set[tuple] = set()
    inbound: dict[int, int] = {}
    for conn in info.connections:
        if conn.source_id not in node_ids:
            problems.append(
                {"severity": "error", "message": f"connection from unknown node {conn.source_id}"}
            )
        if conn.dest_id not in node_ids:
            problems.append(
                {"severity": "error", "message": f"connection to unknown node {conn.dest_id}"}
            )
        key = (conn.source_id, conn.source_port, conn.dest_id, conn.dest_port)
        if key in seen:
            problems.append(
                {
                    "severity": "warning",
                    "message": f"duplicate connection {conn.source_id}->{conn.dest_id}",
                }
            )
        seen.add(key)
        inbound[conn.dest_id] = inbound.get(conn.dest_id, 0) + 1
        if conn.source_id == conn.dest_id:
            problems.append({"severity": "error", "message": "node connected to itself"})

    for node in info.nodes:
        if node.id not in inbound and info.nodes and len(info.nodes) > 1:
            # sources are fine; only flag nodes that nothing feeds but that
            # are not obvious sources -- left to the caller, so just informational
            pass

    if len(ids) != len(info.nodes):
        problems.append({"severity": "error", "message": "duplicate node ids"})
    return problems


def installed_bundles(knime_home: Path) -> set[str]:
    """Symbolic names of every plug-in present in the KNIME installation."""
    names: set[str] = set()
    plugins = Path(knime_home) / "plugins"
    if not plugins.exists():
        return names
    for entry in plugins.iterdir():
        stem = entry.name
        if stem.endswith(".jar"):
            stem = stem[:-4]
        # strip the version suffix: name_1.2.3.v20240101-...
        base = stem.split("_", 1)[0]
        names.add(base)
    return names


# -- import / export (.knwf) --------------------------------------------------

def export_knwf(workflow_path: Path, dest: Path) -> Path:
    """Zip a workflow folder into a ``.knwf`` archive.

    KNIME's own export wraps the workflow in a **top-level folder named after
    the workflow**, e.g.::

        MyWorkflow/workflow.knime
        MyWorkflow/Some Node (#1)/settings.xml

    Putting the contents at the archive root produces a file KNIME will not
    import, so we reproduce that folder wrapper here.
    """
    workflow_path, dest = Path(workflow_path), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = workflow_path.name or dest.stem
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(workflow_path.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(workflow_path)
            if any(part in _IGNORED_DIRS for part in rel.parts):
                continue
            if rel.name == "knime.log":
                continue
            zf.write(item, (Path(prefix) / rel).as_posix())
    return dest


def import_knwf(archive: Path, dest_dir: Path, overwrite: bool = False) -> Path:
    """Unpack a ``.knwf``/``.knar``-style zip into ``dest_dir``.

    Tolerates archives that wrap everything in a single top-level folder.
    """
    archive, dest_dir = Path(archive), Path(dest_dir)
    if dest_dir.exists() and any(dest_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{dest_dir} already exists (pass overwrite=True)")
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        top = {n.split("/", 1)[0] for n in names}
        prefix = ""
        if "workflow.knime" not in names and len(top) == 1:
            candidate = f"{next(iter(top))}/"
            if any(n.startswith(candidate) and n.endswith("workflow.knime") for n in names):
                prefix = candidate
        for member in zf.infolist():
            name = member.filename
            if prefix and not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            if not relative or relative.endswith("/"):
                continue
            target = dest_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                out.write(src.read())

    if not is_workflow_dir(dest_dir):
        # maybe one more level of nesting
        for child in dest_dir.iterdir():
            if child.is_dir() and is_workflow_dir(child):
                return child
        raise ValueError(f"{archive} does not contain a KNIME workflow")
    return dest_dir


# -- logs ---------------------------------------------------------------------

def read_log(workflow_path: Path, max_bytes: int = 64_000) -> str:
    """Tail of ``knime.log`` inside the workflow folder (if present)."""
    log = Path(workflow_path) / "knime.log"
    if not log.exists():
        return ""
    data = log.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def iter_node_settings(workflow_path: Path) -> Iterable[tuple[str, Path]]:
    """Yield ``(node folder name, settings.xml path)`` for every node."""
    workflow_path = Path(workflow_path)
    for child in sorted(workflow_path.iterdir()):
        if child.is_dir() and (child / "settings.xml").is_file():
            yield child.name, child / "settings.xml"
