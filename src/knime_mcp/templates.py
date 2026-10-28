"""Node templates: reusable, parameterisable ``settings.xml`` skeletons.

Creating a KNIME node from scratch means reproducing every settings key the
node's factory expects -- realistically impossible for arbitrary nodes.  What
*is* possible is to harvest a real, working ``settings.xml`` from a workflow
someone already configured, keep it as a template, and then vary it.

A template is a folder::

    templates/csv_reader/
        settings.xml      # the harvested file, untouched
        template.json     # metadata: id, factory, ports, params, description

Parameters declare *which* entries a caller is expected to change.  Every
parameter maps to one dotted path inside the settings tree
(``model.settings.file_selection.path.path``); ``overrides`` in a build
request can additionally set **any** entry by path, which makes the registry
extensible without touching code.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import xmlconfig as xc

_BUNDLED = Path(__file__).parent / "templates"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")


@dataclass
class Param:
    name: str
    path: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str = ""
    #: Paths blanked whenever this param is written -- keeps paired settings
    #: (e.g. the "exclude" half of a dual list) consistent with it.
    clear_on_set: list[str] = field(default_factory=list)
    #: For ``type == "records"``: record key -> array name under ``path``.
    #: Transposes ``[{"column": "A", ...}]`` into KNIME's parallel arrays
    #: (``columnNames``, ``aggregationMethod``, ...).  Unlisted keys map 1:1.
    records_map: dict[str, str] = field(default_factory=dict)
    #: ``(source_param, fill)`` -- when ``source_param`` was applied and this
    #: param was *not* given, rewrite this param as ``[fill] * len(source)``.
    #: Keeps KNIME's parallel arrays (sortOrder vs incllist) in lockstep.
    sync: list[Any] | None = None

    def coerce(self, value: Any) -> Any:
        if value is None:
            return None
        if self.type == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type == "integer":
            return int(value)
        if self.type == "float":
            return float(value)
        if self.type in ("list", "array", "records"):
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("[") or text.startswith("{"):
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, (list, dict)):
                        return parsed
                if self.type == "records":
                    raise ValueError(
                        f"param '{self.name}' expects a JSON array of objects"
                    )
                return [chunk.strip() for chunk in text.split(",") if chunk.strip()]
            if isinstance(value, (tuple, set)):
                return list(value)
            return value
        return str(value)


@dataclass
class Template:
    id: str
    node_name: str
    factory: str
    settings_text: str
    bundle: str | None = None
    description: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    source: str = ""
    directory: Path | None = None
    #: Params holding file paths: relative values are resolved against the
    #: workflow folder so the node ends up with an absolute path.
    path_params: list[str] = field(default_factory=list)
    #: ``[{"path": ..., "value": ...}]`` applied after params -- used for the
    #: companion settings a path needs (e.g. ``file_system_type = LOCAL``).
    post_set: list[dict] = field(default_factory=list)
    #: Settings configs to blank out before saving (stale cached state).
    clear: list[str] = field(default_factory=list)
    #: Rebuild a data-dependent column-spec block from the real input file.
    #: ``{"spec_path": ..., "path_param": "path", "delimiter_param": ...}``
    rebuild_spec: dict | None = None

    def param(self, name: str) -> Param | None:
        for p in self.params:
            if p.name == name:
                return p
        return None

    # -- rendering ------------------------------------------------------------
    def render(
        self,
        params: Optional[dict[str, Any]] = None,
        overrides: Optional[dict[str, Any]] = None,
        node_name: Optional[str] = None,
        base_dir: Optional[Path] = None,
    ) -> str:
        """Produce a complete ``settings.xml`` for one node instance."""
        params = dict(params or {})
        overrides = dict(overrides or {})
        cfg = xc.loads(self.settings_text)

        # 1. named parameters -------------------------------------------------
        missing = []
        applied_values: dict[str, Any] = {}
        for param in self.params:
            if param.name in params and params[param.name] is not None:
                value = params[param.name]
                if param.name in self.path_params:
                    value = _absolute_path(value, base_dir)
                coerced = param.coerce(value)
                applied_values[param.name] = coerced
                if param.type == "records":
                    _set_records(cfg, param.path, coerced, param.records_map)
                else:
                    _set_value(cfg, param.path, coerced)
                applied = True
            elif param.required and param.default is None:
                missing.append(param.name)
                applied = False
            elif param.default is not None:
                value = param.coerce(param.default)
                if param.name in self.path_params:
                    value = _absolute_path(value, base_dir)
                applied_values[param.name] = value
                if param.type == "records":
                    _set_records(cfg, param.path, value, param.records_map)
                else:
                    _set_value(cfg, param.path, value)
                applied = True
            else:
                applied = False
            if applied:
                for stale in param.clear_on_set:
                    _blank(cfg, stale)
        if missing:
            raise ValueError(
                f"template '{self.id}' missing required parameter(s): {', '.join(missing)}"
            )

        # 1b. keep parallel arrays in lockstep --------------------------------
        for param in self.params:
            if not param.sync or param.name in applied_values:
                continue
            source, fill = param.sync
            if source not in applied_values:
                continue
            source_value = applied_values[source]
            if isinstance(source_value, (list, tuple)):
                _set_value(cfg, param.path, [fill] * len(source_value))
                applied_values[param.name] = [fill] * len(source_value)

        # 2. free-form overrides ---------------------------------------------
        for key, value in overrides.items():
            _set_value(cfg, key, value, create=True)

        # 3. template constants ------------------------------------------------
        for item in self.post_set:
            _set_value(cfg, item["path"], item.get("value"), create=True)

        # 4. drop stale cached state ------------------------------------------
        for path in self.clear:
            _blank(cfg, path)

        # 5. regenerate data-dependent schema blocks --------------------------
        # Harvested templates carry the *source instance's* column specs, which
        # do not describe the file we are actually pointing at.  KNIME fails the
        # node (and the whole workflow) if that stale type map disagrees with
        # the real input, so rebuild it from the file itself.
        if self.rebuild_spec:
            applied_values = _rebuild_spec(cfg, self.rebuild_spec, applied_values)

        # A node we just materialised is always "configured, not yet executed".
        cfg.set("state", "CONFIGURED")
        cfg.set("node_file", "settings.xml")
        if node_name:
            cfg.set("name", node_name)
            cfg.set("node-name", node_name)

        # Executed source nodes may point at cached data that we did not copy.
        for stale in ("file_store_id", "file_store_location"):
            entry = cfg.find_entry(stale)
            if entry is not None:
                entry.value = None
                entry.isnull = True

        return xc.dumps(cfg)

    def to_dict(self, include_defaults: bool = True) -> dict:
        data = {
            "id": self.id,
            "node_name": self.node_name,
            "factory": self.factory,
            "bundle": self.bundle,
            "description": self.description,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "required": p.required,
                    "description": p.description,
                    **({"default": p.default} if include_defaults and p.default is not None else {}),
                    **({"clear_on_set": p.clear_on_set} if p.clear_on_set else {}),
                    **({"records_map": p.records_map} if p.records_map else {}),
                    **({"sync": p.sync} if p.sync else {}),
                }
                for p in self.params
            ],
        }
        if self.path_params:
            data["path_params"] = list(self.path_params)
        if self.post_set:
            data["post_set"] = list(self.post_set)
        if self.clear:
            data["clear"] = list(self.clear)
        if self.rebuild_spec:
            data["rebuild_spec"] = dict(self.rebuild_spec)
        if self.source:
            data["source"] = self.source
        return data


def _coerce_like(entry: xc.Entry, value: Any) -> Any:
    if value is None:
        return None
    if entry.type == "xboolean":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if entry.type in ("xint", "xlong"):
        return int(value)
    if entry.type == "xdouble":
        return float(value)
    return str(value)


def _set_path(cfg: xc.Config, dotted: str, value: Any, create: bool = False) -> None:
    """Set ``dotted`` in ``cfg``, creating intermediate configs if asked to."""
    entry = cfg.find_entry(dotted)
    if entry is not None:
        entry.value = _coerce_like(entry, value)
        entry.isnull = value is None
        return
    if not create:
        raise KeyError(f"settings path not found in template: {dotted}")
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get_config(part)
        if child is None:
            child = xc.Config(key=part)
            node.children.append(child)
        node = child
    node.set(parts[-1], value)


def _parent_config(cfg: xc.Config, dotted: str, create: bool = False) -> tuple[xc.Config, str]:
    """Walk to the config *containing* ``dotted``'s last segment."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get_config(part)
        if child is None:
            if not create:
                raise KeyError(f"settings path not found in template: {dotted}")
            child = xc.Config(key=part)
            node.children.append(child)
        node = child
    return node, parts[-1]


def _set_value(cfg: xc.Config, dotted: str, value: Any, create: bool = False) -> None:
    """Set ``dotted``; collections become KNIME array configs.

    Arrays in XMLConfig are a config with ``array-size`` plus ``0..n-1``
    indexed entries, so ``["A", "B"]`` written to ``model.cols`` yields
    ``model.cols.array-size = 2``, ``model.cols.0 = A``, ``model.cols.1 = B``.
    A list of dicts becomes an array of *configs* (KNIME's pattern for
    repeated records such as ``renamings.0.oldName``).
    """
    if isinstance(value, (list, tuple, set)):
        parent, key = _parent_config(cfg, dotted, create=create)
        arr = parent.get_config(key)
        if arr is None:
            if not create:
                raise KeyError(f"settings path not found in template: {dotted}")
            arr = xc.Config(key=key)
            parent.children.append(arr)
        # KNIME has two array flavours: with ``array-size`` (SettingsModel) and
        # plain numbered children (hand-rolled configs).  Preserve whichever
        # the template already uses -- the reader code depends on it.
        sample = arr.get_entry("0")
        had_size = arr.get_entry("array-size") is not None
        has_children = any(True for _ in arr.children)
        arr.children.clear()
        values = list(value)
        if had_size or not has_children:
            arr.set("array-size", len(values), type="xint")
        for i, item in enumerate(values):
            if isinstance(item, dict):
                record = xc.Config(key=str(i))
                for rkey, rvalue in item.items():
                    _set_value(record, str(rkey), rvalue, create=True)
                arr.children.append(record)
            elif sample is not None:
                arr.set(str(i), _coerce_like(sample, item))
            else:
                arr.set(str(i), item)
        return
    _set_path(cfg, dotted, value, create=create)


def _find_config(cfg: xc.Config, dotted: str) -> xc.Config | None:
    node: xc.Config | None = cfg
    for part in dotted.split("."):
        if node is None:
            return None
        node = node.get_config(part)
    return node


def _set_records(
    cfg: xc.Config,
    base: str,
    records: list[dict],
    records_map: dict[str, Any],
) -> None:
    """Transpose records into KNIME's parallel-array layout.

    ``[{"column": "A", "method": "Sum"}, ...]`` at base ``model.agg`` with
    ``{"column": "columnNames", "method": "aggregationMethod"}`` writes
    ``model.agg.columnNames = [A, ...]`` and ``model.agg.aggregationMethod = [Sum, ...]``.

    A map value of ``{"array": ..., "wrap": "cell_class"}`` wraps plain values
    into the element config shape the template already uses (its other keys are
    copied as defaults) -- that is how ``columnTypes`` entries
    (``{cell_class, is_null}``) are built from a bare class name.
    """
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError("records parameter expects a list of objects")
    order: list[str] = []
    for record in records:
        for key in record:
            if key not in order:
                order.append(key)
    for key in order:
        spec = records_map.get(key, key)
        if isinstance(spec, dict):
            array_name = spec["array"]
            wrap_key = spec.get("wrap")
        else:
            array_name, wrap_key = spec, None
        values = [record.get(key) for record in records]
        if wrap_key:
            template = _find_config(cfg, f"{base}.{array_name}")
            element = template.get_config("0") if template else None
            defaults = (
                {
                    c.key: c.python_value
                    for c in element.children
                    if isinstance(c, xc.Entry)
                }
                if element is not None
                else {}
            )
            values = [
                v
                if isinstance(v, dict)
                else {**defaults, wrap_key: v}
                for v in values
            ]
        _set_value(cfg, f"{base}.{array_name}", values, create=True)


def _rebuild_spec(cfg: xc.Config, spec: dict, applied: dict) -> dict:
    """Regenerate a harvested node's column-spec block from the real input file.

    Harvested templates carry the source instance's inferred column types,
    which describe *that* file -- not the one we point the new node at.  KNIME
    rejects the node (and the whole workflow) when the type map disagrees with
    the actual input, so we rebuild it by sniffing the target file.

    ``spec`` keys:
        spec_path    dotted path of the container holding the per-column specs
                     (the indexed children are rewritten in place)
        path_param   name of the applied param holding the input file path
        delimiter_param  optional param holding the delimiter (default ",")
        header_param optional boolean param, whether row 1 is a header
    """
    spec_path = spec.get("spec_path")
    path_param = spec.get("path_param", "path")
    source = applied.get(path_param)
    if not spec_path or not isinstance(source, str) or not source:
        return applied
    file_path = Path(source)
    if not file_path.is_file():
        return applied  # let KNIME report a normal "file not found" instead

    delimiter = applied.get(spec.get("delimiter_param", "delimiter"), ",")
    if not isinstance(delimiter, str) or not delimiter:
        delimiter = ","
    first_row_header = applied.get(spec.get("header_param", "first_row_header"), True)
    if not isinstance(first_row_header, bool):
        first_row_header = True

    from . import csv_schema  # local import: only this template kind needs it

    names, types = csv_schema.build_spec(file_path, delimiter, first_row_header)
    if not names:
        return applied

    holder = cfg
    parts = spec_path.split(".")
    for part in parts[:-1]:
        holder = holder.get_config(part)
        if holder is None:
            return applied
    last = parts[-1]
    block = holder.get_config(last)
    if block is None:
        return applied

    # replace every indexed child with the freshly inferred columns
    for child in list(block.children):
        if isinstance(child, xc.Config) and child.key.isdigit():
            block.children.remove(child)
    has_type = bool(spec.get("has_type", False))
    for i, (name, ktype) in enumerate(zip(names, types)):
        entry = xc.Config(key=str(i))
        entry.set("name", name, type="xstring")
        # KNIME requires the "type" key to exist; when hasType is false it is
        # ignored and KNIME re-scans the real file to infer the type.
        entry.set("type", ktype, type="xstring")
        entry.set("hasType", "true" if has_type else "false", type="xboolean")
        block.children.append(entry)

    # The enclosing "specs/N" record still names the *old* file and caches its
    # file-system location.  Point the identifier at the real input and update the
    # cached path so KNIME re-resolves and re-scans it.
    parts = spec_path.split(".")
    # .../specs/0/spec  -> the record is ".../specs/0", its container is ".../specs"
    record = None
    container = cfg
    for p in parts[:-1]:
        container = container.get_config(p)
        if container is None:
            break
    if container is not None:
        record = container  # .../specs/0
    if record is not None:
        sid = record.find_entry("sourceIdentifier")
        if sid is not None:
            sid.value = str(file_path)
        fs_loc = record.get_config("fsLocation")
        if fs_loc is not None:
            for entry_name in ("path", "specifier"):
                e = fs_loc.find_entry(entry_name)
                if e is not None:
                    e.value = str(file_path)

    # ``columnTransformation`` (and ``configId_Internals``) cache per-column
    # production paths for the *old* columns.  It is a sibling of ``specs``
    # (both live directly under ``transformationParameters``), so walk back up
    # from the record to that shared parent.
    tp = None
    walker = cfg
    # strip "<...>/specs/N/spec" to reach the transformationParameters parent
    for p in parts[:-3] if len(parts) >= 3 else []:
        walker = walker.get_config(p)
        if walker is None:
            break
    tp = walker
    ct = tp.get_config("columnTransformation") if tp is not None else None
    if ct is not None:
        _rebuild_column_transformation(ct, names, types)

    # The harvested node was EXECUTED: it carries cached column data and file
    # stores that no longer match the new columns.  Clear them so KNIME treats
    # the node as freshly configured.
    root_entry = cfg.find_entry("hasContent")
    if root_entry is not None:
        root_entry.value = "false"
    store = cfg.get_config("filestores")
    if store is not None:
        for key in ("file_store_location", "file_store_id"):
            e = store.find_entry(key)
            if e is not None:
                e.value = None
                e.isnull = True

    # The cached "sourceId_Internals" still names the file the node was scanned
    # from.  Leaving it stale makes KNIME treat the spec as belonging to a
    # different source, so point it at the real input.
    model = cfg.get_config("model")
    if model is not None:
        internals = model.find_entry("sourceId_Internals")
        if internals is not None:
            internals.value = str(file_path)
    return applied


def _rebuild_column_transformation(ct: xc.Config, names: list[str], types: list[str]) -> None:
    """Rewrite ``columnTransformation`` so it mirrors ``names``/``types``.

    Each entry pairs a ``columnName`` with a production path that turns the
    scanned source type into the KNIME cell type.  Without a coherent entry per
    column KNIME cannot map a spec column to its production path (null
    "position") and refuses the node.
    """
    # KNIME keeps ONE extra trailing entry in columnTransformation -- a
    # "Default columntype" sentinel with a null columnName.  It is not a real
    # column; dropping it makes KNIME index past the end of the array.  Rescue it
    # *before* clearing, then re-append it once the real columns are written.
    sentinel = None
    for child in list(ct.children):
        if not isinstance(child, xc.Config) or not child.key.isdigit():
            continue
        label_entry = child.find_entry("originalTypeLabel")
        if label_entry is not None and label_entry.value == "Default columntype":
            sentinel = child
            break

    # drop every previously harvested column entry, then re-add one per column
    for child in list(ct.children):
        if isinstance(child, xc.Config) and child.key.isdigit():
            ct.children.remove(child)

    for i, (name, ktype) in enumerate(zip(names, types)):
        entry = xc.Config(key=str(i))
        entry.set("columnName", name, type="xstring")
        label, factory = _cs.production_for(ktype)
        orig = _production_path(factory, label, ktype)
        orig.key = "originalProductionPath"
        entry.children.append(orig)
        entry.set("originalTypeLabel", label, type="xstring")
        entry.set("includeInOutput", "true", type="xboolean")
        entry.set("columnRename", name, type="xstring")
        prod = _production_path(factory, label, ktype)
        prod.key = "productionPath"
        entry.children.append(prod)
        ct.children.append(entry)

    if sentinel is not None:
        sentinel.key = str(len(names))
        ct.children.append(sentinel)


from . import csv_schema as _cs


def _production_path(factory: str, label: str, ktype: str) -> xc.Config:
    cfg = xc.Config(key="")
    # converter_name is the bare converter class name (String/Integer/...)
    bare = _cs.KT_TO_NAME.get(ktype, "String")
    cfg.set("_producer", f"class {ktype}->{ktype}", type="xstring")
    cfg.set("_converter_name", bare, type="xstring")
    cfg.children.append(xc.Config(key="_converter_config"))
    cfg.set("_converter_src", ktype, type="xstring")
    cfg.set("_converter_dst", label, type="xstring")
    cfg.set("_producer_src", ktype, type="xstring")
    cfg.set("_producer_name", ktype, type="xstring")
    cfg.set("_producer_dst", ktype, type="xstring")
    cfg.set("_converter", factory, type="xstring")
    cfg.children.append(xc.Config(key="_producer_config"))
    return cfg


def _blank(cfg: xc.Config, dotted: str) -> None:
    """Drop stale state at ``dotted`` (missing paths are ignored).

    Entries become null; configs lose their children but keep an
    ``array-size = 0`` if they were arrays, because KNIME validates that key.
    """
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.get_config(part)
        if node is None:
            return
    last = parts[-1]
    entry = node.get_entry(last)
    if entry is not None:
        entry.value = None
        entry.isnull = True
        return
    child = node.get_config(last)
    if child is None:
        return
    if last.isdigit():
        # array element: remove it outright -- an emptied element would still
        # be iterated by readers of array-size-less arrays (e.g. row filter
        # predicates) and fail validation.
        node.children.remove(child)
        size = node.get_entry("array-size")
        if size is not None:
            remaining = sum(
                1 for c in node.children if isinstance(c, xc.Config) and c.key.isdigit()
            )
            size.value = remaining
        return
    was_array = child.get_entry("array-size") is not None
    child.children.clear()
    if was_array:
        child.set("array-size", 0, type="xint")


def _absolute_path(value: Any, base_dir: Optional[Path]) -> Any:
    """Resolve a relative file path against the workflow folder.

    Absolute paths (and non-strings) pass through untouched; ``~`` and
    environment variables are expanded first.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    import os

    expanded = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(expanded)
    if candidate.is_absolute():
        return str(candidate)
    if base_dir is None:
        return str(candidate)  # leave it relative; KNIME resolves vs cwd
    return str((Path(base_dir) / candidate))


# -- registry -----------------------------------------------------------------

class TemplateRegistry:
    """Loads templates from the package plus any user-supplied folders."""

    def __init__(self, extra_dirs: Iterable[Path] = ()):
        self._dirs = [Path(d) for d in (_BUNDLED, *extra_dirs) if Path(d).exists()]
        self._cache: dict[str, Template] = {}
        self.reload()

    def reload(self) -> None:
        self._cache.clear()
        for directory in self._dirs:
            for child in sorted(directory.iterdir()):
                meta = child / "template.json"
                settings = child / "settings.xml"
                if not meta.is_file() or not settings.is_file():
                    continue
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                template = Template(
                    id=data.get("id", child.name),
                    node_name=data.get("node_name", child.name),
                    factory=data.get("factory", ""),
                    bundle=data.get("bundle"),
                    description=data.get("description", ""),
                    inputs=list(data.get("inputs", [])),
                    outputs=list(data.get("outputs", [])),
                    params=[
                        Param(
                            name=p["name"],
                            path=p["path"],
                            type=p.get("type", "string"),
                            required=bool(p.get("required", False)),
                            default=p.get("default"),
                            description=p.get("description", ""),
                            clear_on_set=list(p.get("clear_on_set", [])),
                            records_map=dict(p.get("records_map", {})),
                            sync=list(p["sync"]) if p.get("sync") else None,
                        )
                        for p in data.get("params", [])
                    ],
                    path_params=list(data.get("path_params", [])),
                    post_set=list(data.get("post_set", [])),
                    clear=list(data.get("clear", [])),
                    rebuild_spec=data.get("rebuild_spec"),
                    source=data.get("source", ""),
                    settings_text=settings.read_text(encoding="utf-8"),
                    directory=child,
                )
                self._cache[template.id] = template

    def ids(self) -> list[str]:
        return sorted(self._cache)

    def get(self, template_id: str) -> Template:
        try:
            return self._cache[template_id]
        except KeyError:
            raise KeyError(
                f"unknown template '{template_id}'. Available: {', '.join(sorted(self._cache)) or '(none)'}"
            ) from None

    def __contains__(self, template_id: str) -> bool:
        return template_id in self._cache

    def search(self, query: str = "") -> list[Template]:
        query = query.strip().lower()
        out = []
        for template in sorted(self._cache.values(), key=lambda t: t.id):
            if not query:
                out.append(template)
                continue
            haystack = " ".join(
                [template.id, template.node_name, template.factory, template.description]
            ).lower()
            if query in haystack:
                out.append(template)
        return out

    def save(self, template: Template, directory: Path) -> Path:
        target = save_template(template, directory)
        self.reload()
        return target


# -- harvesting & inspection --------------------------------------------------

def flat_entries(settings_path: Path) -> dict[str, Any]:
    """Every entry in a node's settings as ``{"dotted.path": value}``."""
    cfg = xc.load(settings_path)
    out: dict[str, Any] = {}
    _walk(cfg, "", out)
    return out


def _walk(cfg: xc.Config, prefix: str, out: dict[str, Any]) -> None:
    for child in cfg.children:
        key = f"{prefix}.{child.key}" if prefix else child.key
        if isinstance(child, xc.Entry):
            out[key] = child.python_value
        else:
            _walk(child, key, out)


def harvest(
    settings_path: Path,
    template_id: str,
    *,
    node_name: Optional[str] = None,
    description: str = "",
    params: Optional[list[dict]] = None,
    inputs: Optional[list[str]] = None,
    outputs: Optional[list[str]] = None,
    source: str = "",
    path_params: Optional[list[str]] = None,
    post_set: Optional[list[dict]] = None,
    clear: Optional[list[str]] = None,
) -> Template:
    """Turn a real node's ``settings.xml`` into a reusable template."""
    if not _ID_RE.match(template_id):
        raise ValueError(
            f"template id '{template_id}' must be lowercase letters/digits/underscore/dash"
        )
    cfg = xc.load(settings_path)
    factory = cfg.get("factory")
    if not factory:
        raise ValueError(f"{settings_path} has no factory entry -- not a node settings file")
    template = Template(
        id=template_id,
        node_name=node_name or cfg.get("node-name") or cfg.get("name") or template_id,
        factory=factory,
        bundle=cfg.get("node-bundle-symbolic-name"),
        description=description,
        inputs=list(inputs or []),
        outputs=list(outputs or []),
        params=[
            Param(
                name=p["name"],
                path=p["path"],
                **{
                    k: v
                    for k, v in p.items()
                    if k in (
                        "type",
                        "required",
                        "default",
                        "description",
                        "clear_on_set",
                        "records_map",
                        "sync",
                    )
                },
            )
            for p in (params or [])
        ],
        path_params=list(path_params or []),
        post_set=list(post_set or []),
        clear=list(clear or []),
        source=source or str(settings_path),
        settings_text=xc.dumps(cfg),
    )
    return template


def save_template(template: Template, directory: Path) -> Path:
    """Write ``template`` into ``directory`` as a template folder.

    Module-level convenience so harvesting scripts don't need a registry
    instance; it shares ``TemplateRegistry.save``'s file layout.
    """
    target = Path(directory) / template.id
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "settings.xml").write_text(template.settings_text, encoding="utf-8")
    meta = {
        "id": template.id,
        "node_name": template.node_name,
        "factory": template.factory,
        "bundle": template.bundle,
        "description": template.description,
        "inputs": template.inputs,
        "outputs": template.outputs,
        "source": template.source,
        "path_params": list(template.path_params),
        "post_set": list(template.post_set),
        "clear": list(template.clear),
        "params": [
            {
                "name": p.name,
                "path": p.path,
                "type": p.type,
                "required": p.required,
                **({"default": p.default} if p.default is not None else {}),
                "description": p.description,
                **({"clear_on_set": p.clear_on_set} if p.clear_on_set else {}),
                **({"records_map": p.records_map} if p.records_map else {}),
                **({"sync": p.sync} if p.sync else {}),
            }
            for p in template.params
        ],
    }
    (target / "template.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


def default_registry(extra_dirs: Iterable[Path] = ()) -> TemplateRegistry:
    """Registry with env/project-config extra template folders merged in."""
    import os

    dirs: list[Path] = list(extra_dirs)
    env_dir = os.environ.get("KNIME_MCP_TEMPLATE_DIR")
    if env_dir:
        dirs.append(Path(env_dir).expanduser())
    cfg_file = Path.cwd() / ".knime-mcp.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            dirs.extend(Path(d) for d in data.get("template_dirs", []))
        except (OSError, json.JSONDecodeError):
            pass
    return TemplateRegistry(dirs)
