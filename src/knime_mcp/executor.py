"""Headless execution of KNIME workflows.

Since KNIME 5.9 the vendor's Batch Executor is gone, so this module supports
two interchangeable *executors* and picks whatever is installed:

``knime-batch``
    The classic ``org.knime.product.KNIME_BATCH_APPLICATION`` (KNIME <= 5.8,
    or an installation that already had the extension before it was pulled).
``nodepit``
    NodePit Batch (``com.nodepit.batch.application.NodePitBatchExecutor``),
    the community drop-in replacement that still ships for 5.12+.

Both are driven the same way: spawn ``knime.exe`` with the right
``-application``, wait, capture output, then re-read the workflow folder to
see what actually happened.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import workspace
from .config import KnimeEnv

KNIME_BATCH_APP = "org.knime.product.KNIME_BATCH_APPLICATION"
NODEPIT_BATCH_APP = "com.nodepit.batch.application.NodePitBatchExecutor"


class ExecutorError(RuntimeError):
    """Raised when a workflow cannot even be started."""


@dataclass
class ExecutionResult:
    ok: bool
    exit_code: int | None
    duration_s: float
    executor: str
    command: list[str]
    stdout: str = ""
    stderr: str = ""
    log: str = ""
    workflow: str = ""
    node_states: list[dict] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    timed_out: bool = False

    def to_dict(self, max_log_chars: int = 6000) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "executor": self.executor,
            "workflow": self.workflow,
            "timed_out": self.timed_out,
            "command": " ".join(_quote(c) for c in self.command),
            "nodes": self.node_states,
            "problems": self.problems,
            "stdout_tail": self.stdout[-max_log_chars:],
            "stderr_tail": self.stderr[-max_log_chars:],
            "knime_log_tail": self.log[-max_log_chars:],
        }


def _quote(part: str) -> str:
    return f'"{part}"' if (" " in part or "(" in part or ")" in part) else part


# -- executor detection -------------------------------------------------------

def detect_executor(env: KnimeEnv) -> str | None:
    """Return ``"knime-batch"``, ``"nodepit"`` or ``None``."""
    return env.executor_kind


def executor_hint(env: KnimeEnv) -> dict:
    kind = detect_executor(env)
    if kind:
        return {"id": kind, "installed": True, "message": f"executor available: {kind}"}
    return {
        "id": None,
        "installed": False,
        "message": (
            "No headless executor is installed. KNIME discontinued its own Batch "
            "Executor for 5.9+; install NodePit Batch instead:"
        ),
        "fix": (
            "knime.exe -nosplash -consoleLog -application org.eclipse.equinox.p2.director "
            "-repository https://download.nodepit.com/<version> "
            "-installIUs com.nodepit.batch.feature.feature.group -d <knime-home>"
        ),
    }


# -- command construction -----------------------------------------------------

def _batch_workspace(env: KnimeEnv) -> Path:
    """A dedicated ``-data`` folder so headless runs never fight the GUI.

    KNIME locks its workspace: running batch execution against the workspace
    the GUI has open fails with "workspace is in use".  Using a sibling
    directory removes that conflict entirely (workflow file paths are
    resolved relative to the workflow folder anyway, via ``knime.workflow``).
    """
    configured = getattr(env, "batch_workspace", None)
    if configured:
        return Path(configured)
    return Path(str(env.workspace) + "-mcp-batch")


def build_command(
    env: KnimeEnv,
    workflow: Path,
    executor: str,
    *,
    save: bool = True,
    reset: bool = True,
    variables: Optional[dict[str, object]] = None,
    credentials: Optional[list[tuple[str, str, str]]] = None,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    exe = str(env.knime_exe)
    data_dir = _batch_workspace(env)
    data_dir.mkdir(parents=True, exist_ok=True)

    if executor == "nodepit":
        # NodePit Batch has no --workspace option: ``-data`` is an Eclipse
        # *launcher* argument, so it must precede ``-application``.
        # CRITICAL: use the space-separated form ``-data <dir>``.  The
        # ``-data=<dir>`` form is rejected by NodePit's picocli argument
        # parser ("Unknown option: '-data=...'") and the run never starts.
        cmd = [
            exe,
            "-nosplash",
            "-consoleLog",
            "-data",
            str(data_dir),
            "-application",
            NODEPIT_BATCH_APP,
        ]
        if not save:
            cmd.append("--no-save")
        if reset:
            cmd.append("-r")
        for name, value in sorted((variables or {}).items()):
            cmd.append(f"-v={name};{value};{_variable_type(value)}")
        for name, login, password in credentials or []:
            parts = [name] + ([login] if login else []) + ([password] if password else [])
            cmd.append("-c=" + ";".join(parts))
        cmd.append(str(workflow))
    else:
        # ``-data`` must also use the space-separated launcher form here.
        cmd = [
            exe,
            "-nosplash",
            "-consoleLog",
            "-noexit",  # keeps the JVM alive until the executor is done
            "-data",
            str(data_dir),
            "-application",
            KNIME_BATCH_APP,
            f"-workflowDir={workflow}",
        ]
        if not save:
            cmd.append("-nosave")
        if reset:
            cmd.append("-reset")
        for name, value in sorted((variables or {}).items()):
            # classic batch executor expects a preferences file instead of -v
            cmd.append(f"-Dknime.workflow.variable.{name}={value}")

    # NOTE: deliberately no ``-vmargs`` here.  Passing -vmargs on the command
    # line *replaces* knime.ini's -vmargs block, which would drop KNIME's
    # required --add-opens flags.  Tune the heap in knime.ini instead.
    cmd += list(env.extra_args)
    cmd += list(extra_args or [])
    return cmd


def _variable_type(value: object) -> str:
    if isinstance(value, bool):
        return "int"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    return "String"


# -- running ------------------------------------------------------------------

def _terminate_tree(process: subprocess.Popen) -> None:
    """Kill the launcher *and* the JVM it spawned."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            process.terminate()
        except OSError:
            pass


def execute(
    workflow: Path,
    env: KnimeEnv,
    *,
    save: bool = True,
    reset: bool = True,
    variables: Optional[dict[str, object]] = None,
    credentials: Optional[list[tuple[str, str, str]]] = None,
    timeout: Optional[int] = None,
    executor: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> ExecutionResult:
    """Run ``workflow`` headlessly and report what happened."""
    workflow = Path(workflow).resolve()
    if not (workflow / "workflow.knime").exists():
        raise ExecutorError(f"not a KNIME workflow folder: {workflow}")

    kind = executor or detect_executor(env)
    if kind is None:
        hint = executor_hint(env)
        raise ExecutorError(hint["message"] + "\n  " + hint["fix"])

    cmd = build_command(
        env,
        workflow,
        kind,
        save=save,
        reset=reset,
        variables=variables,
        credentials=credentials,
        extra_args=extra_args,
    )
    timeout = timeout or env.execution_timeout

    # KNIME writes knime.log into the workflow folder; remember whether it
    # existed so we can tell new content apart.
    started = time.time()
    env.knime_home.mkdir(parents=True, exist_ok=True)

    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(env.knime_home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise ExecutorError(f"could not start {env.knime_exe}: {exc}") from exc

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - very stuck JVM
            stdout, stderr = "", ""
    duration = time.time() - started

    info = workspace.describe(workflow)
    log_text = workspace.read_log(workflow)
    failed_nodes = [n for n in info.nodes if n.state not in ("CONFIGURED", "EXECUTED")]

    problems: list[dict] = []
    exit_code = process.returncode
    if timed_out:
        problems.append(
            {"severity": "error", "message": f"execution timed out after {timeout}s"}
        )
    if not info.nodes:
        problems.append({"severity": "error", "message": "workflow has no nodes"})

    # NodePit Batch documents its exit codes; the classic executor just uses
    # non-zero for failure.
    exit_messages = {
        10: "invalid command line input",
        20: "the workflow could not be loaded",
        30: "the workflow execution failed",
        40: "the workflow could not be saved",
        50: "an unknown error occurred",
    }
    if exit_code in exit_messages:
        problems.append(
            {"severity": "error", "message": f"executor reported: {exit_messages[exit_code]}"}
        )
    elif exit_code not in (0, None):
        problems.append({"severity": "error", "message": f"executor exit code {exit_code}"})

    # KNIME's own log is the most reliable source for *why* something broke.
    log_errors = _log_error_lines(log_text)
    for line in log_errors[:15]:
        problems.append({"severity": "error", "message": line})

    for node in failed_nodes:
        problems.append(
            {
                "severity": "error",
                "node": node.folder,
                "message": f"node ended in state {node.state!r}: {node.error or 'execution did not complete'}",
            }
        )
    problems.extend(p for p in info.problems if p.get("severity") == "error")

    ok = not timed_out and exit_code == 0 and not problems

    return ExecutionResult(
        ok=ok,
        exit_code=exit_code,
        duration_s=duration,
        executor=kind,
        command=cmd,
        stdout=stdout or "",
        stderr=stderr or "",
        log=log_text,
        workflow=str(workflow),
        node_states=[
            {"id": n.id, "name": n.name, "folder": n.folder, "state": n.state}
            for n in info.nodes
        ],
        problems=problems,
        timed_out=timed_out,
    )


def _log_error_lines(log_text: str, limit: int = 15) -> list[str]:
    """Pull the interesting ERROR lines out of KNIME's ``knime.log``."""
    if not log_text:
        return []
    out: list[str] = []
    for raw in log_text.splitlines():
        line = raw.strip()
        if " ERROR " not in line and not line.startswith("ERROR "):
            continue
        # drop the framework noise, keep the message
        if "StatusLogger" in line or "log4j" in line.lower():
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def usage(env: KnimeEnv, executor: Optional[str] = None, timeout: int = 60) -> str:
    """Ask the installed executor to print its own CLI usage."""
    kind = executor or detect_executor(env)
    if kind is None:
        raise ExecutorError(executor_hint(env)["message"])
    cmd = [str(env.knime_exe), "-nosplash", "-consoleLog", "-application",
           NODEPIT_BATCH_APP if kind == "nodepit" else KNIME_BATCH_APP]
    if kind == "nodepit":
        cmd.append("-h")  # classic batch executor prints usage with no args
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(env.knime_home),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "(executor did not answer a usage request in time)"
    return (proc.stdout or "") + (proc.stderr or "")


def install_executor(env: KnimeEnv, kind: str = "nodepit") -> str:
    """Best-effort unattended install of an executor (p2 director)."""
    if kind == "knime-batch":
        raise ExecutorError(
            "KNIME's own Batch Executor is no longer distributed for 5.9+. "
            "Use kind='nodepit'."
        )
    version = _knime_version(env)
    repo = f"https://download.nodepit.com/{version}"
    cmd = [
        str(env.knime_exe),
        "-nosplash",
        "-consoleLog",
        "-application",
        "org.eclipse.equinox.p2.director",
        f"-repository={repo}",
        "-installIUs=com.nodepit.batch.feature.feature.group",
        f"-d={env.knime_home}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(env.knime_home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _knime_version(env: KnimeEnv) -> str:
    plugins = env.knime_home / "plugins"
    if plugins.exists():
        for entry in plugins.iterdir():
            if entry.name.startswith("org.knime.core_"):
                version = entry.name[len("org.knime.core_"):].split(".v")[0]
                parts = version.split(".")
                if len(parts) >= 2:
                    return f"{parts[0]}.{parts[1]}"
    return "5.12"
