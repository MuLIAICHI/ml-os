"""Fire-once-per-transition macOS notifications for pending gates.

The :class:`Notifier` owns the "did I already notify about this gate?" bookkeeping and
the actual send. It is deliberately never handed the list of watched repos — the only
path it ever writes is its own configured ``state_file`` — so it structurally cannot
write into a watched repo (task-2 reframing of the read-only property, R-T2-a).

Fire-once rule: a gate notifies when its identity key first appears; never again while
it stays pending; again only after it clears and reopens (or is re-presented with a new
``since``). Gate identity = ``(repo, task, gate_type, since_canonical)``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

from mlos.reader import Gate

log = logging.getLogger(__name__)

GateKey = tuple[str, str, str, str]


def _applescript_str(s: str) -> str:
    """Quote and escape ``s`` for safe embedding in an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Notifier:
    """Tracks which gates have been notified and fires a notification on new ones."""

    def __init__(
        self,
        state_file: Path | str,
        backend: str = "osascript",
        send: Callable[[Gate], None] | None = None,
        open_url: str | None = None,
    ) -> None:
        """Create a notifier.

        Args:
            state_file: Path to ml-os's own last-notified state (JSON). Never a repo path.
            backend: ``"osascript"`` (default) or ``"terminal-notifier"``.
            send: Optional injected sender (used by tests); defaults to the real macOS send.
            open_url: URL a clicked notification opens (terminal-notifier only; osascript
                notifications cannot carry a click action and land in Script Editor).
        """
        self._state_file = Path(state_file)
        self._backend = backend
        self._open_url = open_url
        self._send: Callable[[Gate], None] = send or self._default_send
        self._notified, self._had_state = self._load()

    @property
    def had_state(self) -> bool:
        """True if a (readable) state file existed when this notifier was created."""
        return self._had_state

    def _load(self) -> tuple[set[GateKey], bool]:
        """Load the persisted notified-key set. A missing/malformed file → empty, no state."""
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return {tuple(k) for k in data}, True
        except FileNotFoundError:
            return set(), False
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("notifier state unreadable (%s); treating as empty", exc)
            return set(), False

    def _save(self) -> None:
        """Persist the notified-key set. A write failure is logged, never fatal."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = [list(k) for k in sorted(self._notified)]
            self._state_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.error("could not persist notifier state to %s: %s", self._state_file, exc)

    def prime(self, gates: list[Gate]) -> None:
        """Seed the notified set from ``gates`` WITHOUT firing (silent first-run seed)."""
        self._notified = {g.key for g in gates}
        self._save()

    def sync(self, gates: list[Gate]) -> list[Gate]:
        """Fire for every gate whose key is new, then persist. Returns the fired gates.

        Cleared gates drop out of ``gates`` and therefore leave the notified set, so they
        can fire again if they reopen. Still-pending gates stay in the set, so a poll never
        re-notifies them.
        """
        current: dict[GateKey, Gate] = {g.key: g for g in gates}
        new = [g for key, g in current.items() if key not in self._notified]
        for gate in new:
            try:
                self._send(gate)
            except Exception as exc:  # a notification failure must never break the watcher
                # Deliberate: the gate is still marked notified below (no retry storm), so a
                # transient failure loses that one notification rather than re-firing forever.
                log.error("notification send failed for %s: %s", gate.key, exc)
        self._notified = set(current.keys())
        self._save()
        return new

    def build_command(self, gate: Gate) -> list[str]:
        """Build the notification argv for ``gate`` (split out so tests can assert on it)."""
        title = f"{gate.repo} · {gate.gate_type} gate"
        body = f"task {gate.task} waiting since {gate.since_canonical}"
        if self._backend == "terminal-notifier":
            # -group: one notification per repo·task — a re-presented gate replaces the
            # stale banner instead of stacking. -open: click lands in the Gate Inbox.
            cmd = [
                "terminal-notifier", "-title", title, "-message", body,
                "-group", f"ml-os.{gate.repo}.{gate.task}",
            ]
            if self._open_url:
                cmd += ["-open", self._open_url]
            return cmd
        script = f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"
        return ["osascript", "-e", script]

    def _default_send(self, gate: Gate) -> None:
        """Fire a real macOS notification via osascript / terminal-notifier."""
        # No shell=True; args passed as a list. check=False so a nonzero exit doesn't raise.
        subprocess.run(self.build_command(gate), check=False, timeout=10, capture_output=True)
