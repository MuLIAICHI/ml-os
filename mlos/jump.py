"""Jump-to-session: focus the session manager's workspace for a repo (task 5).

Read-only + jump (spec): this focuses/reveals a workspace; it never injects input into
the session and never opens a watched repo's files (read or write). ml-os's only file
write remains the notifier state file.

Two strategies, selected by ``[session_manager].kind``:

* ``cmux`` (default) — the correct focus is a *resolve*, not an open:
  ``cmux workspace list --json`` exposes each workspace's ``current_directory``; we match
  it to the repo path and ``cmux workspace select <ref>`` to make it active. (``cmux open
  <path>`` was rejected — it opens the directory as a file preview, not the waiting
  session.) If no workspace matches, fall back to ``cmux <path>`` to open one.
* ``command`` — run a config-driven argv template (``focus_command`` with ``{path}`` /
  ``{repo}`` placeholders) — for herdr deep-links or any custom launcher.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

from mlos.config import Config, Repo

log = logging.getLogger(__name__)

CMUX_BIN = "cmux"

# Runner signature: (argv, capture) -> stdout str if capture else None.
Runner = Callable[[list[str], bool], "str | None"]


class JumpError(Exception):
    """Raised only when the jump target repo cannot be resolved (unknown repo)."""


def _default_run(argv: list[str], capture: bool) -> str | None:
    """Run ``argv`` with no shell; return stdout when ``capture`` else None. Never raises on exit code."""
    result = subprocess.run(
        argv, shell=False, check=False, timeout=10, capture_output=True, text=True
    )
    return result.stdout if capture else None


def build_focus_command(config: Config, repo: Repo) -> list[str]:
    """Substitute ``{path}``/``{repo}`` into the configured focus_command argv (command kind).

    Per-element substitution so a repo path/name with spaces stays one argv element.
    """
    template = config.session_manager["focus_command"]
    subs = {"{path}": str(repo.path), "{repo}": repo.name}
    argv: list[str] = []
    for arg in template:
        for token, value in subs.items():
            arg = arg.replace(token, value)
        argv.append(arg)
    return argv


def _match_workspace_ref(workspaces: list[dict], repo_path: Path) -> str | None:
    """Return the ref/id of the workspace whose cwd is (or is inside) ``repo_path``.

    Exact ``current_directory`` match wins; otherwise a workspace whose cwd is nested
    inside the repo (an agent that cd'd into a subdir). Returns None if nothing matches.
    """
    for ws in workspaces:
        cd = ws.get("current_directory")
        if cd and Path(cd) == repo_path:
            return ws.get("ref") or ws.get("id")
    for ws in workspaces:
        cd = ws.get("current_directory")
        if cd:
            try:
                Path(cd).relative_to(repo_path)  # cwd is inside the repo
                return ws.get("ref") or ws.get("id")
            except ValueError:
                continue
    return None


def _cmux_focus(repo: Repo, run: Runner) -> None:
    """Focus the existing cmux workspace for ``repo`` (or open one if none is live)."""
    out = run([CMUX_BIN, "workspace", "list", "--json"], True) or ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        log.error("could not parse cmux workspace list: %s", exc)
        return
    ref = _match_workspace_ref(data.get("workspaces", []), repo.path)
    if ref:
        run([CMUX_BIN, "workspace", "select", ref], False)
    else:
        # No live workspace for this repo — open one at its path (launches cmux if needed).
        log.info("no cmux workspace at %s; opening a new one", repo.path)
        run([CMUX_BIN, str(repo.path)], False)


def focus(config: Config, repo_name: str, run: Runner | None = None) -> None:
    """Focus the session for ``repo_name``.

    Args:
        config: Loaded configuration.
        repo_name: Registered repo name to focus.
        run: Injected runner (tests); defaults to a no-shell subprocess run.

    Raises:
        JumpError: if ``repo_name`` is not a registered repo (nothing is run).

    Any failure of the focus itself (cmux not running, parse error, bad ref) is logged
    and swallowed — a focus that can't complete must never crash the server.
    """
    repo = next((r for r in config.repos if r.name == repo_name), None)
    if repo is None:
        raise JumpError(f"unknown repo: {repo_name!r}")
    runner: Runner = run or _default_run
    kind = config.session_manager.get("kind", "cmux")
    try:
        if kind == "cmux":
            _cmux_focus(repo, runner)
        else:
            runner(build_focus_command(config, repo), False)
    except Exception as exc:  # best-effort: never propagate a focus failure
        log.error("jump focus failed for %s: %s", repo_name, exc)
