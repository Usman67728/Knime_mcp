"""Locating the local KNIME Analytics Platform installation and workspace.

Configuration is resolved in this order (first hit wins) for every value:

1. Environment variable (``KNIME_MCP_*``).
2. ``.knime-mcp.json`` in the current working directory or any parent.
3. Autodetection from the KNIME installation itself.
4. A built-in default.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_FILE = ".knime-mcp.json"

#: Locations where the Windows installer likes to put ``knime.exe``.
_KNIME_GLOBS = (
    r"C:\Program Files\KNIME*\knime.exe",
    r"C:\Program Files (x86)\KNIME*\knime.exe",
    r"%LOCALAPPDATA%\Programs\KNIME\knime.exe",
    r"%USERPROFILE%\knime*\knime.exe",
    r"D:\KNIME*\knime.exe",
    r"C:\knime*\knime.exe",
)


def _expand(pattern: str) -> str:
    return os.path.expandvars(pattern)


def find_knime_home() -> Optional[Path]:
    """Return the directory containing ``knime.exe`` (or ``knime`` on POSIX)."""
    explicit = os.environ.get("KNIME_MCP_HOME") or os.environ.get("KNIME_HOME")
    if explicit:
        path = Path(explicit).expanduser()
        if (path / "knime.exe").exists() or (path / "knime").exists():
            return path
        if path.name.lower().startswith("knime") and path.is_file():
            return path.parent
        raise FileNotFoundError(f"KNIME_MCP_HOME does not look like a KNIME install: {path}")

    import glob
    import shutil

    for pattern in _KNIME_GLOBS:
        for match in sorted(glob.glob(_expand(pattern))):
            candidate = Path(match)
            if candidate.is_file():
                return candidate.parent
    # fall back to PATH
    for name in ("knime.exe", "knime"):
        found = shutil.which(name)
        if found:
            return Path(found).parent
    return None


def _recent_workspaces(knime_home: Path) -> list[Path]:
    """Read the workspaces KNIME itself remembers (``org.eclipse.ui.ide.prefs``)."""
    prefs = knime_home / "configuration" / ".settings" / "org.eclipse.ui.ide.prefs"
    if not prefs.exists():
        return []
    text = prefs.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^RECENT_WORKSPACES=(.*)$", text, re.MULTILINE)
    if not match:
        return []
    sep_match = re.search(r"^RECENT_WORKSPACES_SEPARATOR=(.*)$", text, re.MULTILINE)
    separator = sep_match.group(1) if sep_match else "\\n"
    raw = match.group(1)
    if separator and separator != "\\n":
        raw = raw.replace(separator, "\n")
    else:
        raw = raw.replace("\\n", "\n")
    # Eclipse stores preferences with java.util.Properties escaping
    raw = raw.replace("\\:", ":").replace("\\\\", "\\")
    return [Path(line.strip()) for line in raw.splitlines() if line.strip()]


def _project_file_config() -> dict:
    here = Path.cwd()
    for directory in [here, *here.parents]:
        candidate = directory / CONFIG_FILE
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


@dataclass
class KnimeEnv:
    """Resolved paths and tunables for talking to the local KNIME install."""

    knime_home: Path
    workspace: Path
    #: Extra CLI args appended to every headless run (e.g. ``-vmargs`` block).
    extra_args: list[str] = field(default_factory=list)
    #: Hard limit for a single headless execution, in seconds.
    execution_timeout: int = 1800
    #: Heap override for headless runs, e.g. ``"4g"`` (empty = KNIME default).
    max_heap: str = ""
    #: Separate Eclipse ``-data`` folder used for headless runs, so they never
    #: lock horns with the GUI's workspace. ``None`` = ``<workspace>-mcp-batch``.
    batch_workspace: Optional[Path] = None

    @property
    def knime_exe(self) -> Path:
        for name in ("knime.exe", "knime"):
            candidate = self.knime_home / name
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"knime executable not found in {self.knime_home}")

    # -- capabilities ---------------------------------------------------------
    @property
    def executor_kind(self) -> str | None:
        """``"nodepit"``, ``"knime-batch"`` or ``None``.

        KNIME's own executor was discontinued for 5.9+, so the community
        NodePit Batch extension is the usual answer these days.
        """
        plugins = self.knime_home / "plugins"
        if not plugins.exists():
            return None
        names = [p.name for p in plugins.iterdir()]
        if any(n.startswith("com.nodepit.batch") for n in names):
            return "nodepit"
        if any(n.startswith("org.knime.ap.batch") for n in names):
            return "knime-batch"
        return None

    @property
    def batch_executor_installed(self) -> bool:
        """Any usable headless executor (NodePit or the legacy batch one)."""
        return self.executor_kind is not None

    def missing_capabilities(self) -> list[dict]:
        problems: list[dict] = []
        if self.executor_kind is None:
            problems.append(
                {
                    "id": "executor",
                    "severity": "error",
                    "message": (
                        "No headless executor is installed, so workflows cannot be "
                        "executed. KNIME discontinued its own Batch Executor for 5.9+ "
                        "(April 2026); NodePit Batch is the drop-in replacement."
                    ),
                    "fix": (
                        "knime.exe -nosplash -consoleLog -application "
                        "org.eclipse.equinox.p2.director "
                        "-repository https://download.nodepit.com/<version> "
                        "-installIUs com.nodepit.batch.feature.feature.group "
                        f"-d {self.knime_home}"
                    ),
                }
            )
        if not self.workspace.exists():
            problems.append(
                {
                    "id": "workspace",
                    "severity": "error",
                    "message": f"Workspace does not exist: {self.workspace}",
                    "fix": "Set KNIME_MCP_WORKSPACE to the workspace folder KNIME opens.",
                }
            )
        return problems

    def describe(self) -> dict:
        return {
            "knime_home": str(self.knime_home),
            "knime_exe": str(self.knime_exe),
            "workspace": str(self.workspace),
            "workspace_exists": self.workspace.exists(),
            "batch_executor_installed": self.batch_executor_installed,
            "execution_timeout": self.execution_timeout,
            "problems": self.missing_capabilities(),
        }


def load_env() -> KnimeEnv:
    """Resolve the :class:`KnimeEnv` for this process."""
    file_cfg = _project_file_config()

    knime_home = find_knime_home()
    if knime_home is None:
        raise FileNotFoundError(
            "Could not find a KNIME installation. Set KNIME_MCP_HOME to the folder "
            "that contains knime.exe."
        )

    workspace: Optional[Path] = None
    explicit = (
        os.environ.get("KNIME_MCP_WORKSPACE")
        or os.environ.get("KNIME_WORKSPACE")
        or file_cfg.get("workspace")
    )
    if explicit:
        workspace = Path(explicit).expanduser()
    else:
        remembered = _recent_workspaces(knime_home)
        for candidate in remembered:
            if candidate.exists():
                workspace = candidate
                break
        if workspace is None:
            fallback = Path.home() / "knime-workspace"
            if fallback.exists():
                workspace = fallback
    if workspace is None:
        # last resort: the default workspace name KNIME suggests on first run
        workspace = Path.home() / "knime-workspace"

    return KnimeEnv(
        knime_home=knime_home,
        workspace=workspace,
        extra_args=list(file_cfg.get("extra_args", [])),
        execution_timeout=int(
            os.environ.get("KNIME_MCP_TIMEOUT", file_cfg.get("execution_timeout", 1800))
        ),
        max_heap=str(file_cfg.get("max_heap", os.environ.get("KNIME_MCP_MAX_HEAP", ""))),
        batch_workspace=Path(explicit_batch).expanduser()
        if (explicit_batch := os.environ.get("KNIME_MCP_BATCH_WORKSPACE")
            or file_cfg.get("batch_workspace"))
        else None,
    )


_ENV: Optional[KnimeEnv] = None


def get_env() -> KnimeEnv:
    global _ENV
    if _ENV is None:
        _ENV = load_env()
    return _ENV


def reset_env() -> None:
    """Drop the cached environment (used by tests / after config changes)."""
    global _ENV
    _ENV = None
