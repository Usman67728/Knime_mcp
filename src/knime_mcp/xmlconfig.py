"""Reading and writing KNIME's XMLConfig file format.

KNIME stores every persisted setting (``workflow.knime``, a node's
``settings.xml``, ...) in its own XML flavour called *XMLConfig*::

    <?xml version="1.0" encoding="UTF-8"?>
    <config xmlns="http://www.knime.org/2008/09/XMLConfig" ... key="settings.xml">
        <entry key="name" type="xstring" value="Partitioning"/>
        <config key="model">
            <entry key="fraction" type="xdouble" value="0.8"/>
        </config>
    </config>

Only two element types exist: ``entry`` (a typed leaf value) and ``config``
(a container).  Everything else -- arrays, nulls -- is encoded on top of
those two (an array is a config holding ``array-size`` plus indexed
entries).

This module deliberately avoids :mod:`xml.etree` for *writing*: KNIME is
picky about attribute order and about how control characters are escaped,
and hand-rolled output keeps us byte-compatible with files KNIME itself
produces.  Reading uses ElementTree (safe, because we disable entity
resolution implicitly -- the format has no DTD and no external entities).
"""

from __future__ import annotations

import builtins
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence, Union
from xml.etree import ElementTree as ET

XMLNS = "http://www.knime.org/2008/09/XMLConfig"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOCATION = (
    "http://www.knime.org/2008/09/XMLConfig http://www.knime.org/XMLConfig_2008_09.xsd"
)

#: Value types KNIME's XMLConfig knows about, mapped to python types.
ENTRY_TYPES = ("xstring", "xint", "xlong", "xdouble", "xboolean", "xbase64", "xdate")

_PY_TO_X = {
    bool: "xboolean",
    int: "xint",
    float: "xdouble",
    str: "xstring",
}


@dataclass
class Entry:
    """A leaf value: ``<entry key=... type=... value=.../>``."""

    key: str
    value: Union[str, int, float, bool, None] = None
    type: str | None = None
    isnull: bool = False

    def __post_init__(self) -> None:
        if self.type is None:
            if self.isnull or self.value is None:
                self.type = "xstring"
            else:
                self.type = _PY_TO_X.get(type(self.value), "xstring")

    @property
    def python_value(self) -> Union[str, int, float, bool, None]:
        """The value converted back to a python type based on ``type``."""
        if self.isnull or self.value is None:
            return None
        raw = str(self.value)
        if self.type == "xint" or self.type == "xlong":
            return int(raw)
        if self.type == "xdouble":
            return float(raw)
        if self.type == "xboolean":
            return raw.lower() == "true"
        return raw


@dataclass
class Config:
    """A container: ``<config key=...>...</config>``."""

    key: str
    children: list[Union["Config", Entry]] = field(default_factory=list)

    # -- construction helpers -------------------------------------------------
    def add(self, child: Union["Config", Entry]) -> Union["Config", Entry]:
        self.children.append(child)
        return child

    def set(self, key: str, value, type: str | None = None) -> Entry:
        """Set (or overwrite) a direct child entry.

        ``type`` (the XMLConfig entry type) shadows the builtin here, so the
        value's python type is looked up via ``builtins.type``.
        """
        existing = self.get_entry(key)
        if existing is not None:
            existing.value = value
            existing.isnull = value is None
            if type is not None:
                existing.type = type
            elif value is not None:
                existing.type = _PY_TO_X.get(builtins.type(value), existing.type)
            return existing
        entry = Entry(key=key, value=value, type=type, isnull=value is None)
        self.children.append(entry)
        return entry

    def config(self, key: str, create: bool = True) -> "Config | None":
        """Return the first direct child config with ``key``."""
        for child in self.children:
            if isinstance(child, Config) and child.key == key:
                return child
        if not create:
            return None
        new = Config(key=key)
        self.children.append(new)
        return new

    # -- lookups --------------------------------------------------------------
    def get_entry(self, key: str) -> Entry | None:
        for child in self.children:
            if isinstance(child, Entry) and child.key == key:
                return child
        return None

    def get(self, key: str, default=None):
        entry = self.get_entry(key)
        return default if entry is None else entry.python_value

    def get_config(self, key: str) -> "Config | None":
        for child in self.children:
            if isinstance(child, Config) and child.key == key:
                return child
        return None

    def keys(self) -> list[str]:
        return [c.key for c in self.children]

    def walk(self) -> Iterator["Config"]:
        """Depth-first iteration over this config and all nested configs."""
        yield self
        for child in self.children:
            if isinstance(child, Config):
                yield from child.walk()

    def find_entry(self, dotted: str) -> Entry | None:
        """Look up ``"model.settings.path"`` style key paths."""
        node: Config | None = self
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.get_config(part)
            if node is None:
                return None
        return node.get_entry(parts[-1])

    def find(self, dotted: str, default=None):
        entry = self.find_entry(dotted)
        return default if entry is None else entry.python_value


# -- (de)serialisation -------------------------------------------------------

_ATTR_RE = re.compile(r"[&<>\n\r\t\"]")


_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "\n": "&#10;",
    "\r": "&#13;",
    "\t": "&#9;",
}


def escape(value: str) -> str:
    """Escape a string for use inside an XML attribute value.

    Newlines/CR/tab *must* become character references: XML attribute value
    normalization would otherwise silently turn them into spaces.
    """
    return _ATTR_RE.sub(lambda m: _ESCAPES[m.group(0)], value)


def _format_value(entry: Entry) -> str:
    if entry.isnull or entry.value is None:
        return ""
    if entry.type == "xboolean":
        return "true" if bool(entry.value) else "false"
    if entry.type in ("xint", "xlong"):
        return str(int(entry.value))  # type: ignore[arg-type]
    if entry.type == "xdouble":
        text = repr(float(entry.value))
        # KNIME writes 1.0 rather than 1e+00 style output.
        return text
    return str(entry.value)


def _serialize_entry(entry: Entry, indent: int) -> list[str]:
    pad = "    " * indent
    attrs = [f'key="{escape(entry.key)}"', f'type="{entry.type}"']
    if entry.isnull or entry.value is None:
        attrs.append('isnull="true"')
    attrs.append(f'value="{escape(_format_value(entry))}"')
    return [f"{pad}<entry " + " ".join(attrs) + "/>"]


def _serialize_config(config: Config, indent: int) -> list[str]:
    pad = "    " * indent
    open_tag = f'{pad}<config key="{escape(config.key)}"'
    if not config.children:
        return [open_tag + "/>"]
    lines = [open_tag + ">"]
    for child in config.children:
        if isinstance(child, Entry):
            lines.extend(_serialize_entry(child, indent + 1))
        else:
            lines.extend(_serialize_config(child, indent + 1))
    lines.append(f"{pad}</config>")
    return lines


def dumps(config: Config) -> str:
    """Serialise a :class:`Config` to XMLConfig text."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        f'<config xmlns="{XMLNS}" xmlns:xsi="{XSI}" xsi:schemaLocation="'
        f'{SCHEMA_LOCATION}" key="{escape(config.key)}">'
    )
    for child in config.children:
        if isinstance(child, Entry):
            lines.extend(_serialize_entry(child, 1))
        else:
            lines.extend(_serialize_config(child, 1))
    lines.append("</config>")
    return "\n".join(lines) + "\n"


def _parse_element(elem: ET.Element) -> Union[Config, Entry]:
    # strip the namespace -- tags are either "{ns}entry" or "entry"
    tag = elem.tag.rsplit("}", 1)[-1]
    if tag == "entry":
        return Entry(
            key=elem.get("key", ""),
            value=elem.get("value", ""),
            type=elem.get("type"),
            isnull=elem.get("isnull", "false").lower() == "true",
        )
    children = [_parse_element(child) for child in elem]
    return Config(key=elem.get("key", ""), children=children)


def loads(text: str) -> Config:
    """Parse XMLConfig text into a :class:`Config` tree."""
    root = ET.fromstring(text)  # noqa: S314 - KNIME files, no external entities
    parsed = _parse_element(root)
    if not isinstance(parsed, Config):
        raise ValueError("XMLConfig root element must be <config>")
    return parsed


def load(path: Union[str, Path]) -> Config:
    return loads(Path(path).read_text(encoding="utf-8"))


def save(config: Config, path: Union[str, Path]) -> None:
    Path(path).write_text(dumps(config), encoding="utf-8")


# -- small helpers used all over the code base --------------------------------

def make_array(config: Config, key: str, values: Sequence) -> Config:
    """Write a KNIME array (``array-size`` + indexed entries)."""
    arr = config.config(key)
    arr.children.clear()
    arr.set("array-size", len(values), type="xint")
    for i, value in enumerate(values):
        arr.set(str(i), value)
    return arr


def read_array(config: Config, key: str) -> list:
    arr = config.get_config(key)
    if arr is None:
        return []
    size = int(arr.get("array-size", 0) or 0)
    return [arr.get(str(i)) for i in range(size)]
