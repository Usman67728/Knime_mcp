"""Scan a local KNIME Analytics Platform install and build a node catalog.

Reads ``plugin.xml`` from every bundle in ``<knime>/plugins`` (both ``*.jar``
and unpacked directories), extracts the node contributions of the
``org.knime.workbench.repository.nodes`` extension point, and resolves each
node's category path against the ``org.knime.workbench.repository.categories``
tree declared by the same bundles.

Three outputs are produced:

* ``data/nodes_catalog.json`` -- every node type the install can instantiate.
* ``data/top100_nodes.json``  -- a curated top-100 of the most useful nodes.
* a stdout report cross-referencing the top-100 against the harvested
  templates in ``src/knime_mcp/templates``.

    python scripts/scan_nodes.py                  # scan + rank + report
    python scripts/scan_nodes.py --only-catalog   # skip the ranking phase
    python scripts/scan_nodes.py --top 250        # wider curated list

Standard library only, and KNIME is never launched -- everything is read
straight off disk.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLUGINS = Path(r"C:\Users\chusm\AppData\Local\Programs\KNIME\plugins")
DEFAULT_WORKSPACE = Path(r"C:\Users\chusm\knime-workspace")
DEFAULT_TEMPLATES = REPO_ROOT / "src" / "knime_mcp" / "templates"
DEFAULT_DATA = REPO_ROOT / "data"

NODE_EXTENSION = "org.knime.workbench.repository.nodes"
CATEGORY_EXTENSION = "org.knime.workbench.repository.categories"

# settings.xml keys written by KNIME for every configured node instance.
_SET_FACTORY_RE = re.compile(r'<entry key="factory" type="xstring" value="([^"]+)"')
_SET_NAME_RE = re.compile(r'<entry key="node-name"[^>]*value="([^"]*)"')
_SET_BUNDLE_RE = re.compile(r'<entry key="node-bundle-symbolic-name"[^>]*value="([^"]*)"')

# Factory suffixes that carry no meaning for a human-readable node name.
_FACTORY_SUFFIXES = ("NodeFactory", "NodeDialog", "NodeModel", "Factory")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def local_name(tag: str) -> str:
    """``{http://www.w3.org/2005/Plugin}node`` -> ``node``."""
    return tag.rsplit("}", 1)[-1]


def child_text(elem: ET.Element, *names: str) -> str:
    """Text of the first direct child matching any of ``names`` (ns-agnostic)."""
    wanted = {n.lower() for n in names}
    for kid in elem:
        if local_name(kid.tag).lower() in wanted:
            return (kid.text or "").strip()
    return ""


def clean_text(value: str) -> str:
    """Strip markup and collapse whitespace in a description."""
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )
    return re.sub(r"\s+", " ", value).strip()


def is_true(value: str | None) -> bool:
    return str(value).strip().lower() == "true"


def normalize_name(value: str) -> str:
    """Lowercase alphanumerics only -- used for fuzzy curated-name matching."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def prettify_level(level_id: str) -> str:
    """``io-other`` -> ``Io Other`` (fallback for unregistered categories)."""
    return " ".join(w.capitalize() for w in re.split(r"[-_]", level_id) if w)


def name_from_factory(factory: str) -> str:
    """Best-effort display name when KNIME does not tell us one.

    KNIME 5.x stores no ``<name>`` in ``plugin.xml``; the real name lives in
    the factory's Java source.  Splitting the class name gives a readable
    approximation that is explicitly flagged via ``name_source``.
    """
    simple = factory.rsplit(".", 1)[-1]
    # Strip a trailing class-name version digit *before* the suffix, otherwise
    # "ConstantValueColumnNodeFactory2" keeps its suffix.
    simple = re.sub(r"\d+$", "", simple)
    for suffix in _FACTORY_SUFFIXES:
        if simple.endswith(suffix) and len(simple) > len(suffix):
            simple = simple[: -len(suffix)]
            break
    # KNIME spells "one to many" as One2Many / Many2One in class names.
    simple = re.sub(r"(?<=[a-z])2(?=[A-Z])", "To", simple)
    words = [re.sub(r"\d+$", "", w) for w in _CAMEL_RE.findall(simple)]
    # "Pivot2" / "Unpivot2" -> the digit is a class version, not part of the
    # node name.
    words = [w for w in words if w]
    if not words:
        return factory.rsplit(".", 1)[-1]
    name = " ".join(w if w.isupper() else w[0].upper() + w[1:] for w in words)
    return name


# --------------------------------------------------------------------------
# Bundle reading
# --------------------------------------------------------------------------


def parse_manifest(text: str) -> dict[str, str]:
    """Parse an OSGi MANIFEST.MF, honouring 72-column continuation lines."""
    headers: dict[str, str] = {}
    last: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and last:
            headers[last] += line[1:].strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            last = None
            continue
        last = key.strip()
        headers[last] = value.strip()
    return headers


def read_member(jar: zipfile.ZipFile, name: str) -> bytes | None:
    """Read ``name`` from a jar, tolerating a case-different spelling."""
    names = jar.namelist()
    if name in names:
        return jar.read(name)
    lowered = name.lower()
    for candidate in names:
        if "/" not in candidate and candidate.lower() == lowered:
            return jar.read(candidate)
    return None


def load_bundle(bundle: Path) -> tuple[bytes | None, dict[str, str]]:
    """Return ``(plugin.xml bytes, manifest headers)`` for one bundle."""
    manifest_text = ""
    if bundle.is_dir():
        xml_path = bundle / "plugin.xml"
        xml_bytes = xml_path.read_bytes() if xml_path.is_file() else None
        mf_path = bundle / "META-INF" / "MANIFEST.MF"
        if mf_path.is_file():
            manifest_text = mf_path.read_text(encoding="utf-8", errors="replace")
    else:
        xml_bytes = None
        try:
            with zipfile.ZipFile(bundle) as jar:
                xml_bytes = read_member(jar, "plugin.xml")
                mf = read_member(jar, "META-INF/MANIFEST.MF")
                if mf is not None:
                    manifest_text = mf.decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return None, {}
    return xml_bytes, parse_manifest(manifest_text) if manifest_text else {}


def bundle_identity(headers: dict[str, str], fallback: str) -> tuple[str, str]:
    """``(Bundle-SymbolicName, Bundle-Version)`` without OSGi qualifiers."""
    symbolic = headers.get("Bundle-SymbolicName", "").split(";")[0].strip()
    if not symbolic and headers.get("Bundle-Name"):
        symbolic = fallback.split("_")[0]
    return symbolic or fallback.split("_")[0], headers.get("Bundle-Version", "")


# --------------------------------------------------------------------------
# Phase 1 -- catalog
# --------------------------------------------------------------------------


class CategoryTree:
    """Resolves ``category-path`` ids such as ``/io/read`` into ``IO/Read``."""

    def __init__(self) -> None:
        # level path ("/manipulation/column") -> display name ("Column")
        self._names: dict[str, str] = {}
        # display path -> level path, used as a fallback for legacy catalogues
        self._by_display: dict[str, str] = {}

    def add(self, level_id: str, name: str, parent: str) -> None:
        if not level_id or not name:
            return
        parent = "/" + parent.strip().strip("/")
        level_path = "/" + level_id if parent == "/" else parent + "/" + level_id
        # First declaration wins; core bundles load before extensions.
        self._names.setdefault(level_path, name)
        display_key = "/" + "/".join(normalize_name(p) for p in name.split("/"))
        self._by_display.setdefault(display_key, level_path)

    def resolve(self, category_path: str) -> tuple[str, str, list[str]]:
        """Return ``(full_category_path, top_level_category, segments)``."""
        raw = category_path.strip().strip("/")
        if not raw:
            return "", "", []
        # Legacy bundles spell the path with display names, not level ids.
        display_key = "/" + "/".join(normalize_name(p) for p in raw.split("/"))
        raw = self._by_display.get(display_key, raw)

        names: list[str] = []
        prefix = ""
        for segment in raw.split("/"):
            if not segment:
                continue
            prefix += "/" + segment
            names.append(self._names.get(prefix) or prettify_level(segment))
        return "/".join(names), (names[0] if names else ""), names


def scan_bundles(plugins_dir: Path, verbose: bool = True) -> tuple[list[dict], dict]:
    """Walk every bundle and collect its node contributions."""
    tree = CategoryTree()
    raw_nodes: list[dict] = []
    stats = Counter()

    entries = sorted(
        p for p in plugins_dir.iterdir()
        if p.is_dir() or p.suffix.lower() == ".jar"
    )

    # Pass 1: categories, so every node path resolves against the full tree.
    parsed: list[tuple[ET.Element, str, str, Path]] = []
    for bundle in entries:
        stats["bundles"] += 1
        if bundle.is_dir():
            stats["bundles_unpacked"] += 1
        else:
            stats["bundles_jars"] += 1
        xml_bytes, headers = load_bundle(bundle)
        if xml_bytes is None:
            stats["bundles_without_plugin_xml"] += 1
            continue
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            stats["bundles_unparseable"] += 1
            continue
        stats["bundles_with_plugin_xml"] += 1
        symbolic, version = bundle_identity(headers, bundle.name)
        parsed.append((root, symbolic, version, bundle))

        for ext in root.iter():
            if local_name(ext.tag) != "extension":
                continue
            if ext.get("point") != CATEGORY_EXTENSION:
                continue
            stats["category_elements"] += 1
            for cat in ext:
                if local_name(cat.tag) != "category":
                    continue
                tree.add(
                    cat.get("level-id", ""),
                    clean_text(child_text(cat, "name") or cat.get("name", "")),
                    cat.get("path", "/"),
                )

    # Pass 2: nodes.
    for root, symbolic, version, bundle in parsed:
        for ext in root.iter():
            if local_name(ext.tag) != "extension" or ext.get("point") != NODE_EXTENSION:
                continue
            for node in ext:
                if local_name(node.tag) != "node":
                    continue
                stats["node_elements"] += 1
                attrs = node.attrib
                factory = (
                    attrs.get("factory-class")
                    or child_text(node, "factory", "factoryClass")
                    or ""
                )
                if not factory:
                    stats["nodes_without_factory"] += 1
                    continue
                deprecated = is_true(attrs.get("deprecated")) or bool(
                    child_text(node, "deprecated")
                )
                raw_nodes.append({
                    "factory": factory,
                    "category_path": (
                        attrs.get("category-path")
                        or child_text(node, "fullCategoryPath", "full-category-path")
                    ),
                    "deprecated": deprecated,
                    "hidden": is_true(attrs.get("hidden")),
                    "bundle": symbolic,
                    "version": version,
                    "bundle_source": bundle.name,
                })

    for node in raw_nodes:
        full, top, _ = tree.resolve(node["category_path"])
        node["full_category_path"] = full or "Uncategorized"
        node["category"] = top or "Uncategorized"
        node["name"] = ""
        node["name_source"] = "factory-class"
        node["description"] = ""

    if verbose:
        print(f"[scan] {stats['bundles']} bundles "
              f"({stats['bundles_jars']} jars, {stats['bundles_unpacked']} unpacked)")
        print(f"[scan] {stats['bundles_with_plugin_xml']} had a readable plugin.xml, "
              f"{stats['category_elements']} category extensions")
        print(f"[scan] {stats['node_elements']} <node> elements, "
              f"{stats['nodes_without_factory']} skipped (no factory)")
    return raw_nodes, dict(stats)


def mine_workspace(workspace: Path, verbose: bool = True) -> dict:
    """Harvest real node instances from the user's own workflows.

    This is the only *real* usage signal available: KNIME writes one
    ``settings.xml`` per configured node, recording the factory, the node name
    and the contributing bundle.  ``Example Workflows`` ships with KNIME, so it
    is counted separately and weighted lower.
    """
    user_counts: Counter[str] = Counter()
    example_counts: Counter[str] = Counter()
    names: dict[str, Counter[str]] = defaultdict(Counter)
    bundles: dict[str, str] = {}

    if not workspace.is_dir():
        if verbose:
            print(f"[workspace] {workspace} not found -- skipping usage signal")
        return {"user": {}, "examples": {}, "names": {}, "bundles": {},
                "projects": {}, "user_instances": 0, "example_instances": 0}

    projects: Counter[str] = Counter()
    for project in sorted(workspace.iterdir()):
        if not project.is_dir() or project.name == ".metadata":
            continue
        for settings in project.rglob("settings.xml"):
            try:
                text = settings.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _SET_FACTORY_RE.search(text)
            if not match:
                continue
            factory = match.group(1)
            name_match = _SET_NAME_RE.search(text)
            if name_match and name_match.group(1).strip():
                names[factory][name_match.group(1).strip()] += 1
            bundle_match = _SET_BUNDLE_RE.search(text)
            if bundle_match:
                bundles.setdefault(factory, bundle_match.group(1).split(";")[0])
            if project.name == "Example Workflows":
                example_counts[factory] += 1
            else:
                user_counts[factory] += 1
                projects[project.name] += 1

    if verbose:
        total_user = sum(user_counts.values())
        total_ex = sum(example_counts.values())
        print(f"[workspace] {total_user} node instances in "
              f"{len(projects)} user projects ({len(user_counts)} distinct factories)")
        print(f"[workspace] {total_ex} node instances in KNIME's bundled "
              f"Example Workflows ({len(example_counts)} distinct factories)")
    return {
        "user": dict(user_counts),
        "examples": dict(example_counts),
        "names": {k: dict(v) for k, v in names.items()},
        "bundles": bundles,
        "projects": dict(projects),
        "user_instances": sum(user_counts.values()),
        "example_instances": sum(example_counts.values()),
    }


def find_usage_counters(workspace: Path) -> tuple[dict | None, str]:
    """Locate KNIME's own ``nodeusage_3.0.json`` usage statistics."""
    candidates = [
        workspace / ".metadata" / "knime" / "nodeusage_3.0.json",
        workspace / ".metadata" / "knime" / "nodeusage.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        nodes = (data.get("nodestats") or {}).get("nodes") or []
        return data, str(path)
    return None, ""


def finalize_catalog(raw_nodes: list[dict], usage: dict, verbose: bool = True) -> list[dict]:
    """Dedupe by factory, attach authoritative names, sort for stable output."""
    real_names: dict[str, str] = {}
    for factory, counts in usage["names"].items():
        # Most frequent real node name wins; alphabetically first on a tie.
        real_names[factory] = sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0])
        )[0][0]

    best: dict[str, dict] = {}
    dupes = 0
    for node in raw_nodes:
        factory = node["factory"]
        if factory in real_names:
            node["name"] = real_names[factory]
            node["name_source"] = "workspace-settings"
        if not node["name"]:
            node["name"] = name_from_factory(factory)
            node["name_source"] = "factory-class"
        node["bundle"] = usage["bundles"].get(factory) or node["bundle"]

        current = best.get(factory)
        if current is None:
            best[factory] = node
            continue
        dupes += 1
        # Prefer an active, visible, well-categorised declaration.
        def rank(entry: dict) -> tuple:
            return (
                not entry["deprecated"],
                not entry["hidden"],
                entry["full_category_path"] != "Uncategorized",
                len(entry["category_path"]) > len(current["category_path"]),
            )
        if rank(node) > rank(current):
            best[factory] = node

    catalog = list(best.values())
    catalog.sort(key=lambda n: (n["full_category_path"], n["name"], n["factory"]))
    if verbose:
        named = sum(1 for n in catalog if n["name_source"] == "workspace-settings")
        print(f"[catalog] {len(catalog)} unique node types "
              f"({dupes} duplicate declarations collapsed)")
        print(f"[catalog] {named} names taken from real workspace instances, "
              f"{len(catalog) - named} derived from the factory class name")
    return catalog


# --------------------------------------------------------------------------
# Phase 2 -- curated ranking
# --------------------------------------------------------------------------

# Well-known, frequently used KNIME nodes, ordered by general usefulness and
# deliberately interleaved across families so that no single category can crowd
# the ranking out -- a Views node such as the Histogram sits at the same depth
# as a Manipulation node like the Ranker.
# The names are the display names KNIME actually uses in this install (read
# back from data/nodes_catalog.json), so they match reliably.  ``fc`` gives
# factory-class fragments for nodes whose name here is derived from the class
# rather than copied from KNIME.  Entries that are not installed are reported
# and skipped -- they simply earn no bonus.
CURATED: list[dict] = [
    # --- the core data-prep spine -------------------------------------------
    {"name": "CSV Reader"}, {"name": "Column Filter"}, {"name": "Row Filter"},
    {"name": "GroupBy"}, {"name": "Sorter"}, {"name": "Column Renamer"},
    {"name": "Math Formula"}, {"name": "String Manipulation"},
    {"name": "Rule Engine"}, {"name": "Missing Value"},
    {"name": "Joiner"}, {"name": "Concatenate"}, {"name": "Normalizer"},
    {"name": "Binner"}, {"name": "Column Aggregator"},
    {"name": "Constant Value Column"}, {"name": "Column Appender"},
    {"name": "Column Merger"}, {"name": "One to Many"}, {"name": "Rank"},
    {"name": "Ungroup"}, {"name": "Unpivot"}, {"name": "Pivot"},
    {"name": "Duplicate Row Filter"}, {"name": "Extract Table Dimension"},
    {"name": "Transpose Table"}, {"name": "Cell Splitter"},
    {"name": "Case Convert"}, {"name": "Column Auto Type Caster"},
    {"name": "String To Number"}, {"name": "Number to String"},
    # --- IO: the read/write pair ---------------------------------------------
    {"name": "Excel Reader"}, {"name": "Excel Writer"},
    {"name": "CSV Writer"}, {"name": "Table Writer"},
    {"name": "Table Reader"}, {"name": "File Reader"},
    {"name": "Table Creator"}, {"name": "PMML Writer"}, {"name": "PMML Reader"},
    {"name": "Column Resorter"},
    # --- filters, rules, joins --------------------------------------------------
    {"name": "Rule-based Row Filter"}, {"name": "Rule-based Row Splitter"},
    {"name": "Expression Row Filter"}, {"name": "Cross Joiner"},
    {"name": "Value Lookup"}, {"name": "Regex Split"},
    {"name": "Table Cropper"}, {"name": "Table Splitter"},
    {"name": "Cell Extractor"}, {"name": "Cell Updater"},
    {"name": "Row Aggregator"}, {"name": "Extract Table Spec"},
    {"name": "RowID"}, {"name": "Column Header Extractor"},
    # --- statistics & clustering -------------------------------------------------
    {"name": "Statistics"}, {"name": "Value Counter"},
    {"name": "Numeric Outliers"}, {"name": "Linear Correlation"},
    {"name": "k-Means"}, {"name": "Cluster Assigner"},
    {"name": "Rank Correlation"}, {"name": "Crosstab"},
    {"name": "Numeric Scorer"}, {"name": "Scorer"},
    # --- views: no workflow makes sense without these -------------------------------
    {"name": "Histogram"}, {"name": "Scatter Plotter"}, {"name": "Line Plotter"},
    {"name": "Box Plot"}, {"name": "Interactive Pie"},
    {"name": "Table View", "fc": ["TableViewNodeFactory"]},
    {"name": "Scatter Matrix"}, {"name": "Heat Map"}, {"name": "Line Plot"},
    {"name": "Color Designer (Apply)"}, {"name": "Color Palette Designer"},
    # --- trees & regression ---------------------------------------------------------
    {"name": "Decision Tree Learner"}, {"name": "Decision Tree Predictor"},
    {"name": "Regression Tree Learner"}, {"name": "Regression Tree Predictor"},
    {"name": "Linear Regression Learner"}, {"name": "Regression Predictor"},
    {"name": "Logistic Regression Predictor"},
    {"name": "Tree Ensemble Classification Learner",
     "fc": ["TreeEnsembleClassificationLearner"]},
    {"name": "Tree Ensemble Classification Predictor",
     "fc": ["TreeEnsembleClassificationPredictor"]},
    {"name": "Random Forest Classification Learner",
     "fc": ["RandomForestClassificationLearner"]},
    {"name": "Random Forest Classification Predictor",
     "fc": ["RandomForestClassificationPredictor"]},
    {"name": "Random Forest Regression Learner",
     "fc": ["RandomForestRegressionLearner"]},
    # --- date & time -------------------------------------------------------------------
    {"name": "String To Date Time"}, {"name": "Extract Date Time Fields"},
    {"name": "Date Time Difference"}, {"name": "Date Time To String"},
    {"name": "Modify Date", "fc": ["ModifyDate"]},
    {"name": "Create Date Time", "fc": ["CreateDateTime"]},
    {"name": "Modify Time Zone", "fc": ["ModifyTimeZone"]},
    # --- database ----------------------------------------------------------------------
    {"name": "DB Query Reader"}, {"name": "DBSQL Query"}, {"name": "DB Write"},
    {"name": "DB Table Select", "fc": ["DBTableSelect"]},
    {"name": "DB Connector", "fc": ["DBConnector"]},
    {"name": "DB Group By", "fc": ["DBGroupBy"]},
    # --- scripting & workflow plumbing ----------------------------------------------------
    {"name": "Java Snippet"}, {"name": "Variable Creator"},
    {"name": "Workflow Input"}, {"name": "Workflow Output"},
    {"name": "Container Table Output"}, {"name": "Container Table Input"},
    {"name": "Call Workflow"}, {"name": "Table Manipulator"},
    {"name": "String To Path"}, {"name": "Path To String"},
    {"name": "List Files And Folders"},
    # --- the long tail ------------------------------------------------------------------------
    {"name": "Category to Number"}, {"name": "Column Rename Regex"},
    {"name": "String Replacer"}, {"name": "String Cleaner"},
    {"name": "Cell Splitter By Pos"},
    {"name": "Column Combiner", "fc": ["ColCombine"]},
    {"name": "Column Split", "fc": ["ColumnSplit"]},
    {"name": "Edit Nominal Domain"}, {"name": "Many to One", "fc": ["Many2OneCol"]},
    {"name": "Add Empty Rows"}, {"name": "Top K Selector"}, {"name": "Sampling"},
    {"name": "Shuffle"}, {"name": "Numeric Binner"}, {"name": "Bin By Dictionary"},
    {"name": "Extract Missing Value Cause"}, {"name": "Number Rounder"},
    {"name": "Lag Column"}, {"name": "Multi Column String Manipulation"},
    {"name": "Math Formula (Multi Column)"}, {"name": "Table Row To Image"},
    {"name": "Row to Column Names"}, {"name": "Nominal Value Row Filter"},
    {"name": "Hilite Filter"}, {"name": "Set Operator"},
    # --- more statistics & hypothesis tests ----------------------------------------------------
    {"name": "One Way ANOVA"}, {"name": "Two Sample T Test"},
    {"name": "One Sample T Test"}, {"name": "Paired T Test"}, {"name": "Cronbach"},
    # --- more clustering & dimensionality ------------------------------------------------------
    {"name": "DBSCAN"}, {"name": "Hierarchical Cluster"}, {"name": "K Medoids"},
    {"name": "PCA"}, {"name": "MDS"}, {"name": "Correlation Filter"},
    {"name": "Low Var Filter"}, {"name": "SVM Learner"}, {"name": "SVM Predictor"},
    {"name": "Naive Bayes Learner", "fc": ["NaiveBayesLearner"]},
    # --- more views ------------------------------------------------------------------------------
    {"name": "ROC Curve"}, {"name": "Lift Chart"}, {"name": "Decision Tree View"},
    {"name": "Number Format Manager"}, {"name": "String Format Manager"},
    {"name": "Size Manager"}, {"name": "Shape Manager"},
    {"name": "Paged Table View", "fc": ["PagedTableView"]},
    {"name": "Fixed Column Histogram"}, {"name": "Parallel Coordinate"},
    {"name": "Conditional Box Plot"},
    # --- more scripting & widgets -------------------------------------------------------------------
    {"name": "Java Row Filter"}, {"name": "Java Row Splitter"},
    {"name": "Java Scripting"}, {"name": "String Widget"},
    {"name": "Column Filter Widget", "fc": ["ColumnFilterWidget"]},
    {"name": "File Download Widget"}, {"name": "Text Output Widget"},
    # --- more IO --------------------------------------------------------------------------------------
    {"name": "Model Writer"}, {"name": "Model Reader"},
    {"name": "ARFF Reader"}, {"name": "ARFF Writer"},
    {"name": "Excel Sheet Reader"}, {"name": "Google Sheets Reader"},
    {"name": "Sample Data"}, {"name": "Create Table Structure"},
    {"name": "JSON Reader"}, {"name": "JSON Output"}, {"name": "Table To Json"},
    {"name": "JSON To Table"}, {"name": "String To JSON"}, {"name": "Columns To Json"},
    {"name": "Report Html Writer"}, {"name": "Report Pdf Writer"},
]
# Relative importance of a top-level category, used so that a node outside the
# curated list can still surface if it sits in a central part of the product.
# The keys are the top-level categories actually declared by this install.
CATEGORY_WEIGHT: dict[str, int] = {
    "Manipulation": 200, "IO": 190, "Analytics": 180, "Views": 170,
    "DB": 120, "Scripting": 120, "Other": 90, "Workflow Abstraction": 70,
    "Structured Data": 60, "Reporting": 50, "Workflow Control": 40,
    "Tools & Services": 30, "KNIME Labs": 20,
}
DEFAULT_CATEGORY_WEIGHT = 20

# Scoring weights -- documented in the emitted "notes" field.
W_USER_INSTANCE = 1000.0
W_EXAMPLE_INSTANCE = 25.0
CURATED_TOP_BONUS = 1000.0
CURATED_FLOOR = 100.0


def build_curated_index(catalog: list[dict], verbose: bool = True) -> dict[str, int]:
    """Map factory -> curated rank (lower is better); 0 means 'not curated'."""
    by_norm: dict[str, str] = {}
    for node in catalog:
        by_norm.setdefault(normalize_name(node["name"]), node["factory"])

    index: dict[str, int] = {}
    matched = 0
    unmatched: list[str] = []
    for rank, entry in enumerate(CURATED):
        factory = _match_curated(entry, catalog, by_norm)
        if factory is None:
            unmatched.append(entry["name"])
            continue
        if factory not in index or rank < index[factory]:
            index[factory] = rank
        matched += 1
    if verbose:
        print(f"[rank] curated list: {matched} of {len(CURATED)} entries matched an "
              f"installed node; {len(unmatched)} absent ("
              f"{', '.join(unmatched[:6])}{' ...' if len(unmatched) > 6 else ''})")
    return index


def _match_curated(entry: dict, catalog: list[dict],
                   by_norm: dict[str, str]) -> str | None:
    """Resolve one curated entry to a factory, trying decreasing strictness."""
    needle = normalize_name(entry["name"])
    # 1. Exact normalised name.
    factory = by_norm.get(needle)
    if factory:
        return factory
    # 2. Explicit factory substring hints.
    for fragment in entry.get("fc", []):
        for candidate in (n["factory"] for n in catalog):
            if fragment in candidate:
                return candidate
    # 3. Fuzzy: one name contains the other, but only when both are specific
    #    enough -- otherwise "Table" would swallow "Table Creator".
    if len(needle) >= 6:
        for node in catalog:
            actual = normalize_name(node["name"])
            if len(actual) < 6:
                continue
            shorter, longer = sorted((len(needle), len(actual)))
            if shorter / longer < 0.6:
                continue
            if needle in actual or actual in needle:
                return node["factory"]
    return None


def rank_nodes(catalog: list[dict], usage: dict, curated: dict[str, int],
               top_n: int, verbose: bool = True) -> list[tuple[dict, float, dict]]:
    """Score every node and return the top ``top_n`` as ``(node, score, parts)``."""
    user = usage["user"]
    examples = usage["examples"]
    n_curated = max(len(CURATED), 1)

    scored: list[tuple[dict, float, dict]] = []
    for node in catalog:
        factory = node["factory"]
        u = user.get(factory, 0)
        e = examples.get(factory, 0)
        rank = curated.get(factory)
        curated_bonus = (
            CURATED_TOP_BONUS
            - (rank / n_curated) * (CURATED_TOP_BONUS - CURATED_FLOOR)
        ) if rank is not None else 0.0
        cat_bonus = float(CATEGORY_WEIGHT.get(node["category"], DEFAULT_CATEGORY_WEIGHT))
        score = u * W_USER_INSTANCE + e * W_EXAMPLE_INSTANCE + curated_bonus + cat_bonus
        parts = {
            "workspace_instances": u,
            "example_workflow_instances": e,
            "curated_rank": rank,
            "curated_bonus": round(curated_bonus, 1),
            "category_bonus": cat_bonus,
        }
        scored.append((node, score, parts))

    scored.sort(key=lambda item: (
        -item[1],
        node_sort_key(item[0]),
    ))

    # KNIME ships legacy variants that share a display name with their modern
    # replacement (CSV Reader, Joiner, Normalizer, Table Creator ...).  A
    # "most useful nodes" list should name each node once, so collapse them and
    # keep the active, visible, best-scoring declaration.
    kept: list[tuple[dict, float, dict]] = []
    seen: dict[str, int] = {}
    for item in scored:
        key = normalize_name(item[0]["name"]) or item[0]["factory"]
        previous = seen.get(key)
        if previous is None:
            seen[key] = len(kept)
            kept.append(item)
            continue
        if _prefer_over(item[0], kept[previous][0]):
            kept[previous] = item

    kept.sort(key=lambda item: (-item[1], node_sort_key(item[0])))
    if verbose:
        collapsed = len(scored) - len(kept)
        print(f"[rank] scored {len(scored)} node types; {collapsed} legacy variants "
              f"sharing a name with a modern node were collapsed")
    return kept[:top_n]


def _prefer_over(candidate: dict, current: dict) -> bool:
    """Prefer active, visible nodes over deprecated or hidden duplicates."""
    return (
        not candidate["deprecated"],
        not candidate["hidden"],
        candidate["full_category_path"] != "Uncategorized",
    ) > (
        not current["deprecated"],
        not current["hidden"],
        current["full_category_path"] != "Uncategorized",
    )


def node_sort_key(node: dict) -> tuple:
    return (node["full_category_path"], node["name"], node["factory"])


# --------------------------------------------------------------------------
# Phase 3 -- template cross-reference
# --------------------------------------------------------------------------


def load_template_factories(templates_dir: Path) -> dict[str, str]:
    """Map ``factory class`` -> template id from every ``template.json``."""
    found: dict[str, str] = {}
    if not templates_dir.is_dir():
        return found
    for manifest in sorted(templates_dir.glob("*/template.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        factory = data.get("factory") or ""
        if factory:
            found.setdefault(factory, data.get("id") or manifest.parent.name)
    return found


def report_templates(top: list[dict], templates: dict[str, str]) -> list[dict]:
    """Print template coverage for the top list and return the gaps."""
    covered = [n for n in top if n["factory"] in templates]
    gaps = [n for n in top if n["factory"] not in templates]
    print(f"\n[templates] {len(templates)} harvested templates on disk")
    print(f"[templates] top-{len(top)} coverage: {len(covered)} have a template, "
          f"{len(gaps)} are gaps")
    if gaps:
        print(f"\n[templates] harvesting candidates (no template yet), "
              f"most useful first:")
        for i, node in enumerate(gaps, 1):
            print(f"  {i:>3}. {node['name'][:42]:<42} {node['full_category_path'][:38]}")
    return [
        {"name": n["name"], "category": n["full_category_path"], "factory": n["factory"]}
        for n in gaps
    ]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def public_node(node: dict) -> dict:
    """Project a scan record down to the documented output schema."""
    return {
        "factory": node["factory"],
        "name": node["name"],
        "category": node["category"],
        "full_category_path": node["full_category_path"],
        "description": node["description"],
        "deprecated": node["deprecated"],
        "bundle": node["bundle"],
        "version": node["version"],
        # Additional, documented extras.
        "hidden": node["hidden"],
        "name_source": node["name_source"],
    }


def knime_version(catalog: list[dict]) -> str:
    """Shortest org.knime.core version, e.g. ``5.12.0``."""
    versions = [
        n["version"].split(".v")[0] for n in catalog
        if n["bundle"] == "org.knime.core" and n["version"]
    ]
    return Counter(versions).most_common(1)[0][0] if versions else "unknown"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"[write] {path} ({path.stat().st_size / 1024:.1f} KiB)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plugins", type=Path, default=DEFAULT_PLUGINS)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--templates", type=Path, default=DEFAULT_TEMPLATES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--top", type=int, default=100,
                        help="size of the curated list (default: 100)")
    parser.add_argument("--only-catalog", action="store_true",
                        help="write only the full catalog, skip ranking")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    verbose = not args.quiet
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # ---- Phase 1: catalog -------------------------------------------------
    raw_nodes, stats = scan_bundles(args.plugins, verbose=verbose)
    usage = mine_workspace(args.workspace, verbose=verbose)
    catalog = finalize_catalog(raw_nodes, usage, verbose=verbose)
    version = knime_version(catalog)

    categories = Counter(n["category"] for n in catalog)
    if verbose:
        print(f"\n{'CATEGORY':<28}{'NODES':>8}")
        print("-" * 36)
        for name, count in categories.most_common():
            print(f"{name:<28}{count:>8}")
        print("-" * 36)
        print(f"{'TOTAL':<28}{len(catalog):>8}")
        print(f"  (deprecated: {sum(n['deprecated'] for n in catalog)}, "
              f"hidden/internal: {sum(n['hidden'] for n in catalog)})")

    write_json(args.out_dir / "nodes_catalog.json", {
        "generated": now,
        "knime_version": version,
        "plugins_dir": str(args.plugins),
        "bundles_scanned": stats.get("bundles", 0),
        "bundles_with_plugin_xml": stats.get("bundles_with_plugin_xml", 0),
        "node_count": len(catalog),
        "category_counts": dict(categories.most_common()),
        "nodes": [public_node(n) for n in catalog],
    })

    if args.only_catalog:
        return 0

    # ---- Phase 2: real usage counters, else heuristic ---------------------
    counters, counters_path = find_usage_counters(args.workspace)
    real_counters: dict[str, int] = {}
    if counters:
        for entry in (counters.get("nodestats") or {}).get("nodes") or []:
            if isinstance(entry, dict):
                factory = entry.get("factory") or entry.get("factoryClass") or ""
                hits = entry.get("count") or entry.get("uses") or 0
                if factory and isinstance(hits, (int, float)):
                    real_counters[factory] = int(hits)

    curated_index = build_curated_index(catalog, verbose=verbose)
    ranked = rank_nodes(catalog, usage, curated_index, args.top, verbose=verbose)

    if real_counters:
        ranking_basis = "knime-usage-counters"
        notes = (f"Ranked from KNIME's own node usage counters in {counters_path}.")
    else:
        ranking_basis = "heuristic"
        found = counters_path or "no nodeusage_3.0.json found"
        notes = (
            "HEURISTIC RANKING - no usable KNIME usage counters exist for this "
            f"workspace. KNIME's usage statistics file ({found}) exists but its "
            f"'nodestats.nodes' array is empty and it records "
            f"workflowsOpened={counters.get('workflowsOpened') if counters else 'n/a'}, "
            f"launches={counters.get('launches') if counters else 'n/a'} over "
            f"{counters.get('uptime') if counters else 0} ms of uptime: no workflow "
            "was ever opened through the GUI, so no per-node counter was ever "
            "written. Eclipse .prefs files and combined-preferences.epf were also "
            "searched; they contain no node usage keys.\n"
            "Score = "
            f"{W_USER_INSTANCE:g} x (node instances in the user's own KNIME "
            f"projects, excluding the bundled Example Workflows; "
            f"{usage['user_instances']} instances over "
            f"{len(usage['user'])} distinct factories) "
            f"+ {W_EXAMPLE_INSTANCE:g} x (instances in KNIME's bundled Example "
            f"Workflows: {usage['example_instances']} instances over "
            f"{len(usage['examples'])} factories) "
            f"+ curated bonus {CURATED_TOP_BONUS:g} -> {CURATED_FLOOR:g} decayed by "
            f"position in a {len(CURATED)}-entry list of well-known KNIME nodes "
            f"(all {len(CURATED)} matched an installed node, covering "
            f"{len(curated_index)} distinct factories) "
            f"+ top-level category weight {DEFAULT_CATEGORY_WEIGHT}.."
            f"{max(CATEGORY_WEIGHT.values())}. Ties break on "
            "(full_category_path, name, factory).\n"
            "Node names that appear more than once in the candidate pool (KNIME "
            "ships legacy variants sharing a display name with their modern "
            "replacement, e.g. two CSV Readers) are collapsed to one entry, "
            "keeping the active, non-hidden, best-scoring declaration; that is "
            "why the list contains no deprecated or hidden nodes.\n"
            "Caveats: no usage numbers are real except the workspace instance "
            "counts, which measure how often a node was *placed in a workflow*, "
            "not how often it was executed. Node names come from real "
            "settings.xml records where available and are otherwise derived from "
            "the factory class name ('name_source' marks which). KNIME 5.x "
            "plugin.xml declares no <name>, <fullCategoryPath> or "
            "<shortDescription>, so 'description' is empty for every node and "
            "'full_category_path' is resolved from the repository category tree; "
            f"{sum(1 for n in catalog if n['category'] == 'Uncategorized')} nodes "
            "declare no category-path at all and are listed as 'Uncategorized'."
        )

    top_nodes = [
        {**public_node(node), "rank": i + 1, "score": round(score, 1), **parts}
        for i, (node, score, parts) in enumerate(ranked)
    ]
    write_json(args.out_dir / "top100_nodes.json", {
        "generated": now,
        "ranking_basis": ranking_basis,
        "notes": notes,
        "knime_version": version,
        "node_count": len(top_nodes),
        "nodes": top_nodes,
    })

    if verbose:
        print(f"\n[ranking] basis = {ranking_basis}")
        print(f"{'#':>4}  {'NAME':<40}{'CATEGORY':<34}SCORE")
        print("-" * 92)
        for entry in top_nodes:
            print(f"{entry['rank']:>4}  {entry['name'][:39]:<40}"
                  f"{entry['full_category_path'][:33]:<34}{entry['score']:>7.1f}")

    # ---- Phase 3: template cross-reference --------------------------------
    templates = load_template_factories(args.templates)
    report_templates([n for n, _, _ in ranked], templates)
    return 0


if __name__ == "__main__":
    sys.exit(main())