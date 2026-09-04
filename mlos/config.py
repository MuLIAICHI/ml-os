"""Load and validate ml-os's single config file (``config.toml``).

Config-driven by design: every path (registered repos, vault, host/port, stale
threshold, session manager) lives in the TOML file so nothing is hardcoded. Parsing
uses only the standard library (``tomllib``, Python 3.11+).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# Default jump-to-session command (task 5). argv template; {path}/{repo} substituted.
DEFAULT_FOCUS_COMMAND = ["cmux", "open", "{path}", "--focus", "true"]


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable, or structurally invalid.

    Errors are explicit and loud — never a silent default — so a misconfigured viewer
    fails at startup rather than silently watching the wrong (or no) repos.
    """


@dataclass(frozen=True)
class Repo:
    """A registered repo the viewer watches (read-only)."""

    name: str
    path: Path


@dataclass(frozen=True)
class NotifierCfg:
    """Notification settings.

    ``state_file`` is ml-os's own last-notified bookkeeping file — the only state ml-os
    persists beyond config. It is deliberately outside any watched repo.

    ``open_url`` is what a clicked notification opens (terminal-notifier backend only;
    osascript's ``display notification`` cannot carry a click action). Config-driven, not
    derived from the bind host: ``0.0.0.0`` is a bind address, not a clickable URL.
    """

    backend: str  # "osascript" | "terminal-notifier"
    state_file: Path
    open_url: str | None = None


def _default_notifier() -> NotifierCfg:
    return NotifierCfg(backend="osascript", state_file=Path("~/.ml-os/notified.json").expanduser())


@dataclass(frozen=True)
class Config:
    """Fully-parsed, validated ml-os configuration."""

    host: str
    port: int
    stale_threshold_minutes: int
    vault: Path
    repos: list[Repo]
    session_manager: dict = field(default_factory=dict)
    poll_interval_seconds: int = 5
    notifier: NotifierCfg = field(default_factory=_default_notifier)
    inference_window_minutes: int = 10

    @property
    def stale_threshold_seconds(self) -> float:
        """Stale cutoff in seconds (convenience for age comparisons)."""
        return self.stale_threshold_minutes * 60

    @property
    def inference_window_seconds(self) -> float:
        """Gate-inference window in seconds (unpatched-repo fallback, task 6)."""
        return self.inference_window_minutes * 60


def _resolve(p: str, base: Path) -> Path:
    """Resolve a config path to an absolute :class:`~pathlib.Path`.

    ``~`` is expanded; an already-absolute path is kept; a relative path is resolved
    against ``base`` (the config file's directory) so config-relative paths work
    regardless of the process working directory.
    """
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def _require(mapping: dict, key: str, ctx: str):
    """Return ``mapping[key]`` or raise :class:`ConfigError` naming the missing key."""
    if key not in mapping:
        raise ConfigError(f"missing required key '{key}' in {ctx}")
    return mapping[key]


def load_config(path: str | Path) -> Config:
    """Load, validate, and return the ml-os :class:`Config` from ``path``.

    Args:
        path: Path to ``config.toml``.

    Returns:
        A validated :class:`Config`.

    Raises:
        ConfigError: if the file is missing, is not valid TOML, or is missing a
            required key / has a wrong-typed value.
    """
    path = Path(path)
    base = path.parent  # relative config paths resolve against the config file's dir
    try:
        with open(path, "rb") as fh:  # tomllib requires binary mode
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    server = _require(raw, "server", "config")
    gates = _require(raw, "gates", "config")

    try:
        host = str(_require(server, "host", "[server]"))
        port = int(_require(server, "port", "[server]"))
        stale = int(_require(gates, "stale_threshold_minutes", "[gates]"))
        inference_window = int(gates.get("inference_window_minutes", 10))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"wrong-typed value in config: {exc}") from exc
    if inference_window <= 0:
        raise ConfigError("inference_window_minutes must be positive")

    vault = _resolve(str(_require(raw, "vault", "config")), base)

    raw_repos = _require(raw, "repos", "config")
    if not isinstance(raw_repos, list) or not raw_repos:
        raise ConfigError("[[repos]] must be a non-empty array of tables")

    repos: list[Repo] = []
    for i, r in enumerate(raw_repos):
        if not isinstance(r, dict):
            raise ConfigError(f"[[repos]] entry {i} is not a table")
        name = str(_require(r, "name", f"[[repos]] entry {i}"))
        repo_path = _resolve(str(_require(r, "path", f"[[repos]] entry {i}")), base)
        repos.append(Repo(name=name, path=repo_path))

    session_manager = raw.get("session_manager", {})
    if not isinstance(session_manager, dict):
        raise ConfigError("[session_manager] must be a table")
    # Jump-to-session settings (task 5). kind selects the strategy: "cmux" (resolve the
    # existing workspace and select it) or "command" (run focus_command argv template).
    kind = session_manager.get("kind", "cmux")
    if kind not in ("cmux", "command"):
        raise ConfigError("[session_manager] kind must be 'cmux' or 'command'")
    focus_command = session_manager.get("focus_command", DEFAULT_FOCUS_COMMAND)
    if (
        not isinstance(focus_command, list)
        or not focus_command
        or not all(isinstance(a, str) for a in focus_command)
    ):
        raise ConfigError("[session_manager] focus_command must be a non-empty list of strings")
    session_manager = {**session_manager, "kind": kind, "focus_command": focus_command}

    # Optional: poll interval (top-level) and [notifier] — both default if absent so a
    # task-1-era config still loads.
    try:
        poll_interval = int(raw.get("poll_interval_seconds", 5))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"poll_interval_seconds must be an integer: {exc}") from exc
    if poll_interval <= 0:
        raise ConfigError("poll_interval_seconds must be positive")

    raw_notifier = raw.get("notifier")
    if raw_notifier is None:
        notifier = _default_notifier()
    else:
        if not isinstance(raw_notifier, dict):
            raise ConfigError("[notifier] must be a table")
        backend = str(raw_notifier.get("backend", "osascript"))
        if backend not in ("osascript", "terminal-notifier"):
            raise ConfigError(
                f"[notifier] backend must be 'osascript' or 'terminal-notifier', got {backend!r}"
            )
        state_file_raw = raw_notifier.get("state_file", "~/.ml-os/notified.json")
        open_url = raw_notifier.get("open_url")
        if open_url is not None and (not isinstance(open_url, str) or not open_url.strip()):
            raise ConfigError("[notifier] open_url must be a non-empty string")
        notifier = NotifierCfg(
            backend=backend,
            state_file=_resolve(str(state_file_raw), base),
            open_url=open_url,
        )

    return Config(
        host=host,
        port=port,
        stale_threshold_minutes=stale,
        vault=vault,
        repos=repos,
        session_manager=session_manager,
        poll_interval_seconds=poll_interval,
        notifier=notifier,
        inference_window_minutes=inference_window,
    )
