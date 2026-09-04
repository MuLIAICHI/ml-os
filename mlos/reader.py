"""Read-only reader over the ``.ay/`` state of watched repos.

This module is the enforcement point for ml-os's core security property: it opens
watched-repo paths **for reading only**. There is deliberately no function here that
writes, creates, or deletes anything under a registered repo — the ``.ay/`` filesystem
is a one-way seam (repo -> viewer).

Reads: pending gate markers (task 1) and each repo's BOARD.md / BLOCKERS.md (task 3).
A gate marker's presence means a ``/go`` cycle is waiting for a human; its absence means
the gate was answered.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mlos.config import Repo

log = logging.getLogger(__name__)

# Location of gate markers within a watched repo, relative to the repo root.
GATES_SUBDIR = ".ay/tracking/gates"


@dataclass(frozen=True)
class Gate:
    """A single pending gate surfaced from a watched repo.

    Attributes:
        repo: Registered repo name.
        task: Task id (string, matching the BOARD.jsonl convention).
        gate_type: ``"plan"`` or ``"code"`` — read from the JSON body, never the filename.
        since: When the gate flipped pending (UTC).
        age_seconds: Seconds elapsed between ``since`` and the reference ``now``.
    """

    repo: str
    task: str
    gate_type: str
    since: datetime
    age_seconds: float
    source: str = "patched"  # "patched" (real .gate) | "inferred" (task-6 fallback)

    @property
    def since_canonical(self) -> str:
        """``since`` as the canonical ``%Y-%m-%dT%H:%M:%SZ`` UTC string.

        Matches exactly what ``go.md`` writes, so it round-trips through the notifier
        state file and compares equal after a restart.
        """
        return self.since.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Stable identity for notification bookkeeping.

        Includes ``since_canonical`` so a revise-and-re-present (the gate deleted on a
        "change X" response, then rewritten with a fresh ``since``) reads as a genuine
        new transition rather than the same still-pending gate.
        """
        return (self.repo, self.task, self.gate_type, self.since_canonical)


def _parse_since(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp (``...Z``) into an aware UTC datetime.

    Raises:
        ValueError: if ``value`` is not a parseable timestamp.
    """
    # go.md writes `%Y-%m-%dT%H:%M:%SZ`; normalize the trailing Z for fromisoformat.
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_gate_file(path: Path, repo_name: str, now: datetime) -> Gate | None:
    """Parse one ``*.gate`` file into a :class:`Gate`, or ``None`` if it is unusable.

    A malformed, partially-written, or unreadable gate file is skipped (logged), never
    fatal — one bad marker must not take down the whole page (rule R-T1-d).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:  # read-only
            body = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("skipping unreadable gate file %s: %s", path, exc)
        return None

    if not isinstance(body, dict):
        log.warning("skipping gate file %s: body is not a JSON object", path)
        return None

    task = body.get("task")
    gate_type = body.get("gate")  # type comes from the JSON body, not the filename
    since_raw = body.get("since")
    if task is None or gate_type is None or since_raw is None:
        log.warning("skipping gate file %s: missing task/gate/since", path)
        return None

    try:
        since = _parse_since(str(since_raw))
    except ValueError as exc:
        log.warning("skipping gate file %s: bad 'since' (%s)", path, exc)
        return None

    return Gate(
        repo=repo_name,
        task=str(task),
        gate_type=str(gate_type),
        since=since,
        age_seconds=(now - since).total_seconds(),
    )


def pending_gates(repos: list[Repo], now: datetime) -> list[Gate]:
    """Return all pending gates across ``repos``, sorted oldest-first.

    Args:
        repos: Registered repos to scan.
        now: Reference time for age calculation (injected so callers/tests are
            deterministic). Should be timezone-aware UTC.

    Returns:
        A list of :class:`Gate`, most-stale first (largest ``age_seconds`` first), so
        the longest-blocked work leads the inbox. A repo with no gate markers (or no
        ``.ay/`` directory) simply contributes nothing.
    """
    gates: list[Gate] = []
    for repo in repos:
        gates_dir = repo.path / GATES_SUBDIR
        if not gates_dir.is_dir():
            continue  # repo not initialized / no gates yet — not an error here
        for gate_path in sorted(gates_dir.glob("*.gate")):
            gate = _read_gate_file(gate_path, repo.name, now)
            if gate is not None:
                gates.append(gate)
    gates.sort(key=lambda g: g.age_seconds, reverse=True)
    return gates


# ── Boards & blockers (task 3) ────────────────────────────────────────────────

BOARD_SUBPATH = ".ay/tracking/BOARD.md"
BLOCKERS_SUBPATH = ".ay/tracking/BLOCKERS.md"


@dataclass(frozen=True)
class BoardTask:
    """One row of a repo's BOARD.md pipeline table."""

    num: str
    title: str
    agent: str
    status: str
    blocked_by: str

    @property
    def status_keyword(self) -> str:
        """Normalized status for CSS/class mapping.

        Strips a trailing parenthetical (e.g. ``DONE (bceb592)`` -> ``DONE``) and
        upper-cases, preserving multi-word statuses like ``IN PROGRESS``.
        """
        s = self.status
        idx = s.find("(")
        if idx != -1:
            s = s[:idx]
        return s.strip().upper()


@dataclass(frozen=True)
class Blocker:
    """One row of a repo's BLOCKERS.md table."""

    id: str
    description: str
    task: str
    owner: str
    status: str
    since: str


@dataclass(frozen=True)
class ProjectBoard:
    """A registered repo's rendered pipeline state.

    ``initialized`` is False when the repo has no ``.ay/`` directory (registration order
    must not matter); in that case ``tasks`` and ``blockers`` are empty and no error is raised.
    """

    repo: str
    initialized: bool
    tasks: list[BoardTask]
    blockers: list[Blocker]


def _table_rows(md_text: str) -> list[list[str]]:
    """Split a GitHub-flavored markdown table into per-row cell lists.

    Skips the ``|---|`` separator row; keeps the header (callers filter it by content).
    Non-table lines are ignored. Never raises.
    """
    rows: list[list[str]] = []
    for raw in md_text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Separator row: every cell is only dashes/colons/spaces (and non-empty).
        if cells and all(c and set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows


def _read_table(repo: Repo, subpath: str) -> list[list[str]]:
    """Read a table file under ``repo`` and return its rows; ``[]`` if missing/unreadable."""
    try:
        text = (repo.path / subpath).read_text(encoding="utf-8")  # read-only
    except OSError:
        return []
    return _table_rows(text)


def read_board(repo: Repo) -> list[BoardTask]:
    """Parse ``<repo>/.ay/tracking/BOARD.md`` into task rows (numeric-id rows only)."""
    tasks: list[BoardTask] = []
    for cells in _read_table(repo, BOARD_SUBPATH):
        if len(cells) < 5:
            continue  # ragged/short row — skip, never blank the whole board
        num = cells[0]
        if not num.isdigit():
            continue  # skips the header (#) and any non-task row
        tasks.append(
            BoardTask(num=num, title=cells[1], agent=cells[2], status=cells[3], blocked_by=cells[4])
        )
    return tasks


def read_blockers(repo: Repo) -> list[Blocker]:
    """Parse ``<repo>/.ay/tracking/BLOCKERS.md`` into blocker rows."""
    blockers: list[Blocker] = []
    for cells in _read_table(repo, BLOCKERS_SUBPATH):
        if len(cells) < 6:
            continue
        bid = cells[0]
        if not bid or bid == "#":
            continue  # skip header / empty
        blockers.append(
            Blocker(
                id=bid,
                description=cells[1],
                task=cells[2],
                owner=cells[3],
                status=cells[4],
                since=cells[5],
            )
        )
    return blockers


def project_boards(repos: list[Repo]) -> list[ProjectBoard]:
    """Return one :class:`ProjectBoard` per repo (board + blockers, or not-initialized)."""
    boards: list[ProjectBoard] = []
    for repo in repos:
        if not (repo.path / ".ay").is_dir():
            boards.append(ProjectBoard(repo=repo.name, initialized=False, tasks=[], blockers=[]))
            continue
        boards.append(
            ProjectBoard(
                repo=repo.name,
                initialized=True,
                tasks=read_board(repo),
                blockers=read_blockers(repo),
            )
        )
    return boards


# ── Trace chain: plan → handoffs → commits (task 4) ───────────────────────────

PLANS_SUBDIR = ".ay/plans"
HANDOFFS_SUBPATH = ".ay/tracking/HANDOFFS.md"
BOARD_JSONL_SUBPATH = ".ay/tracking/BOARD.jsonl"

_HANDOFF_KEYS = {
    "To": "to",
    "From": "from_",
    "Task": "task",
    "Date": "date",
    "Summary": "summary",
    "Detail": "detail",
}
_HANDOFF_LINE = re.compile(r"^(To|From|Task|Date|Summary|Detail):\s?(.*)$")


@dataclass(frozen=True)
class PlanFile:
    """One markdown file from a task's plan folder."""

    name: str
    content: str


@dataclass(frozen=True)
class Handoff:
    """A parsed HANDOFFS.md entry (``from_`` avoids the Python keyword)."""

    to: str
    from_: str
    task: str
    date: str
    summary: str
    detail: str


@dataclass(frozen=True)
class Commit:
    """A commit that implemented a task (from a BOARD.jsonl ``done`` event)."""

    ts: str
    commit: str
    title: str


@dataclass(frozen=True)
class TaskChain:
    """The full provenance of one task: plan files, handoffs, and commits."""

    repo: str
    task: str
    plan_files: list[PlanFile]
    handoffs: list[Handoff]
    commits: list[Commit]


def read_plan(repo: Repo, task: str) -> list[PlanFile]:
    """Read the top-level ``*.md`` files of ``<repo>/.ay/plans/task-{task}/`` (sorted)."""
    plan_dir = repo.path / PLANS_SUBDIR / f"task-{task}"
    if not plan_dir.is_dir():
        return []
    files: list[PlanFile] = []
    for p in sorted(plan_dir.glob("*.md")):
        try:
            files.append(PlanFile(name=p.name, content=p.read_text(encoding="utf-8")))
        except OSError as exc:  # unreadable single file — skip, don't blank the chain
            log.warning("skipping unreadable plan file %s: %s", p, exc)
    return files


def _handoff_blocks(md_text: str) -> list[list[str]]:
    """Return the line-blocks between ``---`` delimiters that are OUTSIDE ``` fences.

    Fence-awareness is essential: HANDOFFS.md's schema header contains an example entry
    inside a ``` fence that must never be surfaced as a real handoff.
    """
    blocks: list[list[str]] = []
    in_fence = False
    block: list[str] | None = None
    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if block is not None:
                block.append(line)
            continue
        if not in_fence and stripped == "---":
            if block is None:
                block = []  # entry starts
            else:
                blocks.append(block)  # entry ends
                block = None
            continue
        if block is not None:
            block.append(line)
    return blocks


def _parse_handoff_block(block: list[str]) -> dict[str, str]:
    """Parse a handoff block's ``Key: value`` lines (Detail may span multiple lines)."""
    fields = {v: "" for v in _HANDOFF_KEYS.values()}
    current: str | None = None
    for line in block:
        m = _HANDOFF_LINE.match(line)
        if m:
            current = _HANDOFF_KEYS[m.group(1)]
            fields[current] = m.group(2)
        elif current is not None:
            fields[current] += "\n" + line
    return {k: v.strip() for k, v in fields.items()}


def read_handoffs(repo: Repo, task: str) -> list[Handoff]:
    """Return HANDOFFS.md entries whose ``Task`` equals ``task`` (fence-aware)."""
    try:
        text = (repo.path / HANDOFFS_SUBPATH).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[Handoff] = []
    for block in _handoff_blocks(text):
        f = _parse_handoff_block(block)
        if f["task"] == str(task) and (f["to"] or f["from_"] or f["summary"]):
            out.append(
                Handoff(
                    to=f["to"], from_=f["from_"], task=f["task"],
                    date=f["date"], summary=f["summary"], detail=f["detail"],
                )
            )
    return out


def read_commits(repo: Repo, task: str) -> list[Commit]:
    """Return commits from BOARD.jsonl ``done`` events for ``task`` (skips //, malformed)."""
    try:
        text = (repo.path / BOARD_JSONL_SUBPATH).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[Commit] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(ev.get("task_id")) == str(task) and ev.get("status") == "done" and ev.get("commit"):
            out.append(Commit(ts=str(ev.get("ts", "")), commit=str(ev["commit"]), title=str(ev.get("title", ""))))
    return out


def task_chain(repo: Repo, task: str) -> TaskChain:
    """Assemble the full trace chain for one task."""
    return TaskChain(
        repo=repo.name,
        task=str(task),
        plan_files=read_plan(repo, task),
        handoffs=read_handoffs(repo, task),
        commits=read_commits(repo, task),
    )


# ── Gate inference fallback for unpatched repos (task 6) ──────────────────────

LOCKS_SUBDIR = ".ay/tracking/locks"
GO_MD_SUBPATH = ".claude/commands/go.md"
_LOCK_NAME = re.compile(r"^task-(\d+)\.lock$")
_LOCK_STARTED = re.compile(r"^started:\s*(\S+)\s*$", re.MULTILINE)


def _repo_has_gates(repo: Repo) -> bool:
    """True if the repo has at least one real ``*.gate`` marker right now."""
    gates_dir = repo.path / GATES_SUBDIR
    return gates_dir.is_dir() and any(gates_dir.glob("*.gate"))


def _repo_is_patched(repo: Repo) -> bool:
    """True if the repo's ``go.md`` writes ``.gate`` markers (has the gate-marker patch).

    This is the correct 'is this repo patched?' signal — NOT the mere absence of ``.gate``
    files, which also matches a patched repo that simply has no gate pending right now (an
    agent mid-build). Inference must not fire for such repos, or the tool would report a
    false 'probably waiting' for actively-working patched repos (including ml-os itself).
    """
    try:
        text = (repo.path / GO_MD_SUBPATH).read_text(encoding="utf-8")
    except OSError:
        return False  # no go.md → treat as unpatched (eligible for inference)
    return "tracking/gates/" in text and ".gate" in text


def _plan_is_complete(repo: Repo, task: str) -> bool:
    """A 'complete enough' plan folder exists for the task (has a README)."""
    return (repo.path / PLANS_SUBDIR / f"task-{task}" / "README.md").is_file()


def inferred_gates(repos: list[Repo], now: datetime, window_seconds: float) -> list[Gate]:
    """Infer 'probably waiting' gates for UNPATCHED repos (no ``.gate`` files).

    Heuristic (always labeled ``source="inferred"``, never certain): for an UNPATCHED repo
    (its ``go.md`` does not write ``.gate`` markers) that also has no live ``.gate`` file, an
    active lock ``task-{N}.lock`` whose plan folder is complete and whose lock is held longer
    than ``window_seconds`` (the deterministic quiet-proxy) → an inferred gate. Patched repos
    are skipped entirely (their markers are authoritative; absence = nothing pending), so a
    real gate is never shadowed or double-counted and an idle patched repo never false-fires.
    Missing/unparseable inputs simply yield nothing.
    """
    out: list[Gate] = []
    for repo in repos:
        if _repo_has_gates(repo) or _repo_is_patched(repo):
            continue  # patched repo (or one with a live gate) — never infer; markers rule
        locks_dir = repo.path / LOCKS_SUBDIR
        if not locks_dir.is_dir():
            continue
        for lock_path in sorted(locks_dir.glob("task-*.lock")):
            m = _LOCK_NAME.match(lock_path.name)
            if not m:
                continue
            task = m.group(1)
            if not _plan_is_complete(repo, task):
                continue
            try:
                text = lock_path.read_text(encoding="utf-8")  # read-only
            except OSError:
                continue
            started_match = _LOCK_STARTED.search(text)
            if not started_match:
                continue
            try:
                since = _parse_since(started_match.group(1))
            except ValueError:
                continue
            age = (now - since).total_seconds()
            if age > window_seconds:
                out.append(
                    Gate(
                        repo=repo.name,
                        task=task,
                        gate_type="inferred",
                        since=since,
                        age_seconds=age,
                        source="inferred",
                    )
                )
    return out
