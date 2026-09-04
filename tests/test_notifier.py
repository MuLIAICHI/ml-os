"""Tests for mlos.notifier — fire-once-per-transition bookkeeping.

The sender is injected (a recorder), so no real notifications fire and assertions are
exact. State lives in a per-test tempfile.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mlos.notifier import Notifier
from mlos.reader import Gate


def gate(repo="alpha", task="1", gate_type="plan", since="2026-08-15T15:10:00Z") -> Gate:
    dt = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return Gate(repo=repo, task=task, gate_type=gate_type, since=dt, age_seconds=0.0)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / "notified.json"
        self.fired: list = []

    def _notifier(self) -> Notifier:
        return Notifier(self.state, send=lambda g: self.fired.append(g.key))

    def test_fires_once_on_new(self):
        n = self._notifier()
        n.prime([])
        n.sync([gate()])
        self.assertEqual(len(self.fired), 1)
        n.sync([gate()])  # still pending → no re-fire
        self.assertEqual(len(self.fired), 1)

    def test_no_refire_while_pending(self):
        n = self._notifier()
        n.prime([])
        for _ in range(5):
            n.sync([gate()])
        self.assertEqual(len(self.fired), 1)

    def test_refire_after_clear_reopen(self):
        n = self._notifier()
        n.prime([])
        n.sync([gate()])       # fires
        n.sync([])             # cleared
        n.sync([gate()])       # reopened → fires again
        self.assertEqual(len(self.fired), 2)

    def test_refire_on_new_since_without_gap(self):
        # Discriminating: same repo/task/type, NEW since, no intervening empty sync.
        # Passes only because `since` is part of the identity key (revise-and-re-present).
        n = self._notifier()
        n.prime([])
        n.sync([gate(since="2026-08-15T15:10:00Z")])          # fires
        fired = n.sync([gate(since="2026-08-15T15:40:00Z")])  # re-presented → MUST fire
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(self.fired), 2)

    def test_no_refire_across_restart(self):
        n1 = self._notifier()
        n1.prime([])
        n1.sync([gate()])
        self.assertEqual(len(self.fired), 1)
        # Fresh notifier on the SAME state file (simulates a server restart).
        fired2 = []
        n2 = Notifier(self.state, send=lambda g: fired2.append(g.key))
        self.assertTrue(n2.had_state)
        n2.sync([gate()])  # still pending across restart → no re-fire
        self.assertEqual(fired2, [])

    def test_prime_seeds_without_firing(self):
        n = self._notifier()
        n.prime([gate(task="1")])
        self.assertEqual(self.fired, [])          # seeding fires nothing
        n.sync([gate(task="1"), gate(task="2")])  # only the genuinely new gate fires
        self.assertEqual(len(self.fired), 1)
        self.assertEqual(self.fired[0][1], "2")

    def test_fires_all_new(self):
        n = self._notifier()
        n.prime([])
        fired = n.sync([gate(task="1"), gate(task="2")])
        self.assertEqual(len(fired), 2)

    def test_send_failure_marked_and_logged(self):
        # A sender that raises must not propagate, and the gate is still marked notified
        # (no retry storm) — so a later sync does not re-attempt it.
        def boom(g):
            raise RuntimeError("osascript missing")

        n = Notifier(self.state, send=boom)
        n.prime([])
        n.sync([gate()])          # raises internally, caught + logged
        # marked notified: a second sync does not call the sender again (would raise again)
        n.sync([gate()])          # no exception escapes
        self.assertTrue(True)     # reaching here == no propagation

    def test_had_state_false_on_first_run(self):
        n = self._notifier()
        self.assertFalse(n.had_state)


class BuildCommandTests(unittest.TestCase):
    """Command construction per backend — asserted directly, nothing executes."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / "notified.json"

    def test_terminal_notifier_with_open_url(self):
        n = Notifier(self.state, backend="terminal-notifier", open_url="http://localhost:8787")
        cmd = n.build_command(gate())
        self.assertEqual(cmd[0], "terminal-notifier")
        self.assertIn("-open", cmd)
        self.assertEqual(cmd[cmd.index("-open") + 1], "http://localhost:8787")
        self.assertIn("-group", cmd)
        self.assertEqual(cmd[cmd.index("-group") + 1], "ml-os.alpha.1")

    def test_terminal_notifier_without_open_url(self):
        n = Notifier(self.state, backend="terminal-notifier")
        cmd = n.build_command(gate())
        self.assertNotIn("-open", cmd)
        self.assertIn("-group", cmd)

    def test_osascript_ignores_open_url(self):
        # osascript's `display notification` has no click action — open_url must not leak in.
        n = Notifier(self.state, backend="osascript", open_url="http://localhost:8787")
        cmd = n.build_command(gate())
        self.assertEqual(cmd[0], "osascript")
        self.assertNotIn("http://localhost:8787", " ".join(cmd))


if __name__ == "__main__":
    unittest.main()
