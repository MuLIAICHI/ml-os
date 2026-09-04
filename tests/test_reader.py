"""Tests for mlos.reader — pending-gate reading against the golden fixtures.

All tests use a fixed ``now`` so staleness/age are deterministic.
"""

import unittest
from datetime import datetime, timezone
from pathlib import Path

import tempfile

from mlos.config import Repo, load_config
from mlos.reader import (
    Gate,
    inferred_gates,
    pending_gates,
    project_boards,
    read_blockers,
    read_board,
    read_commits,
    read_handoffs,
    read_plan,
    task_chain,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 8, 15, 15, 20, 0, tzinfo=timezone.utc)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(FIXTURES / "config.test.toml")

    def _gates(self) -> list[Gate]:
        return pending_gates(self.cfg.repos, NOW)

    def test_pending_gates_basic(self):
        gates = self._gates()
        # 3 valid gates: alpha task-1 plan, alpha task-3 code, beta task-2 plan.
        # (beta task-9 is malformed → skipped; gamma has no gates.)
        self.assertEqual(len(gates), 3)
        keys = {(g.repo, g.task, g.gate_type) for g in gates}
        self.assertEqual(
            keys,
            {("alpha", "1", "plan"), ("alpha", "3", "code"), ("beta", "2", "plan")},
        )

    def test_type_from_json_overrides_filename(self):
        # Discriminating case (R-T1-b): filename says "plan" but the JSON body says
        # "code". The reader MUST report the body's type, not the filename's. This is
        # the seam guarantee — a filename-parsing reader would fail here.
        import json
        import tempfile

        from mlos.config import Repo

        gdir = Path(tempfile.mkdtemp()) / ".ay" / "tracking" / "gates"
        gdir.mkdir(parents=True)
        (gdir / "task-5-plan.gate").write_text(
            json.dumps({"task": "5", "gate": "code", "since": "2026-08-15T15:00:00Z"}),
            encoding="utf-8",
        )
        repo_root = gdir.parent.parent.parent  # <tmp> (holds .ay/)
        gates = pending_gates([Repo(name="x", path=repo_root)], NOW)
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0].gate_type, "code")

    def test_sorted_oldest_first(self):
        gates = self._gates()
        ages = [g.age_seconds for g in gates]
        self.assertEqual(ages, sorted(ages, reverse=True))
        self.assertEqual(gates[0].task, "2")  # beta task-2 is the oldest (since 13:00)

    def test_staleness_with_injected_now(self):
        gates = {g.task: g for g in self._gates()}
        threshold = self.cfg.stale_threshold_seconds  # 1800s
        # task-2 since 13:00 → 8400s old → stale; task-1 since 15:10 → 600s → fresh.
        self.assertGreater(gates["2"].age_seconds, threshold)
        self.assertLess(gates["1"].age_seconds, threshold)

    def test_malformed_gate_skipped(self):
        # beta task-9-plan.gate contains non-JSON; it must not appear and must not raise.
        tasks = {g.task for g in self._gates()}
        self.assertNotIn("9", tasks)

    def test_gate_key(self):
        gate = next(g for g in self._gates() if g.task == "1")
        self.assertEqual(gate.key, ("alpha", "1", "plan", "2026-08-15T15:10:00Z"))
        self.assertEqual(gate.since_canonical, "2026-08-15T15:10:00Z")

    def test_repo_with_no_gates(self):
        gamma = [r for r in self.cfg.repos if r.name == "gamma"]
        self.assertEqual(len(gamma), 1)
        self.assertEqual(pending_gates(gamma, NOW), [])

    def test_missing_ay_dir_not_fatal(self):
        from mlos.config import Repo

        ghost = Repo(name="ghost", path=Path("/tmp/definitely-not-a-repo-xyz"))
        self.assertEqual(pending_gates([ghost], NOW), [])


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(FIXTURES / "config.test.toml")
        self.by_name = {r.name: r for r in self.cfg.repos}

    def test_read_board_populated(self):
        tasks = read_board(self.by_name["alpha"])
        # 4 valid task rows; the ragged row and the non-numeric "x" row are skipped.
        self.assertEqual([t.num for t in tasks], ["1", "2", "3", "4"])
        self.assertEqual(tasks[0].title, "Set up auth")
        self.assertEqual(tasks[3].blocked_by, "3")

    def test_board_status_with_commit(self):
        tasks = {t.num: t for t in read_board(self.by_name["alpha"])}
        self.assertEqual(tasks["1"].status, "DONE (abc1234)")
        self.assertEqual(tasks["1"].status_keyword, "DONE")  # parenthetical stripped

    def test_board_multiword_status_preserved(self):
        tasks = {t.num: t for t in read_board(self.by_name["alpha"])}
        self.assertEqual(tasks["2"].status_keyword, "IN PROGRESS")

    def test_read_board_empty(self):
        self.assertEqual(read_board(self.by_name["beta"]), [])

    def test_board_missing_file(self):
        # gamma has .ay/ but no BOARD.md → empty, not an error.
        self.assertEqual(read_board(self.by_name["gamma"]), [])

    def test_read_blockers(self):
        blockers = read_blockers(self.by_name["alpha"])
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].id, "B-01")
        self.assertEqual(blockers[0].task, "4")
        self.assertEqual(blockers[0].status, "OPEN")

    def test_project_boards_all(self):
        boards = {b.repo: b for b in project_boards(self.cfg.repos)}
        self.assertTrue(boards["alpha"].initialized)
        self.assertEqual(len(boards["alpha"].tasks), 4)
        self.assertEqual(len(boards["alpha"].blockers), 1)
        self.assertTrue(boards["beta"].initialized)
        self.assertEqual(boards["beta"].tasks, [])
        self.assertTrue(boards["gamma"].initialized)
        # delta has no .ay/ → not initialized, no error
        self.assertFalse(boards["delta"].initialized)
        self.assertEqual(boards["delta"].tasks, [])

    def test_malformed_rows_skipped(self):
        # Proven via alpha (contains a ragged row + a non-numeric row); a full parse
        # returns only the 4 valid tasks and never raises.
        self.assertEqual(len(read_board(self.by_name["alpha"])), 4)


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(FIXTURES / "config.test.toml")
        self.alpha = next(r for r in self.cfg.repos if r.name == "alpha")

    def test_read_plan(self):
        files = read_plan(self.alpha, "1")
        self.assertEqual([f.name for f in files], ["README.md", "sequence.md"])
        self.assertIn("authentication", files[0].content)

    def test_read_plan_missing(self):
        self.assertEqual(read_plan(self.alpha, "2"), [])  # no plan dir for task 2

    def test_read_handoffs_for_task(self):
        hs = read_handoffs(self.alpha, "1")
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0].summary, "Auth skeleton landed.")
        self.assertEqual(hs[0].from_, "claude")

    def test_handoffs_ignores_fenced_example(self):
        # The schema example (Task 99, inside a ``` fence) must NOT be parsed as an entry.
        self.assertEqual(read_handoffs(self.alpha, "99"), [])

    def test_read_commits(self):
        commits = read_commits(self.alpha, "1")
        self.assertEqual([c.commit for c in commits], ["aaa1111"])  # done only; in_progress skipped

    def test_commits_only_done(self):
        # task 2 in fixtures has only a "ready" event → no commits.
        self.assertEqual(read_commits(self.alpha, "2"), [])

    def test_commits_skips_malformed_and_header(self):
        # BOARD.jsonl has a // header + a non-JSON line; parsing must not raise.
        self.assertEqual([c.commit for c in read_commits(self.alpha, "4")], ["ddd4444"])

    def test_task_chain(self):
        ch = task_chain(self.alpha, "1")
        self.assertEqual(ch.repo, "alpha")
        self.assertEqual(len(ch.plan_files), 2)
        self.assertEqual(len(ch.handoffs), 1)
        self.assertEqual(len(ch.commits), 1)


class InferenceTests(unittest.TestCase):
    WINDOW = 600  # seconds (10 min)

    def setUp(self):
        self.cfg = load_config(FIXTURES / "config.test.toml")

    def _repo(self, *, lock_started=None, plan=True, gate=False, patched=False) -> Repo:
        """Build a temp repo with optional lock/plan/gate/go.md for inference edge cases."""
        root = Path(tempfile.mkdtemp())
        (root / ".ay" / "tracking" / "locks").mkdir(parents=True)
        (root / ".ay" / "tracking" / "gates").mkdir(parents=True)
        if lock_started:
            (root / ".ay" / "tracking" / "locks" / "task-1.lock").write_text(
                f"agent: x\ntask: 1\nstarted: {lock_started}\n", encoding="utf-8"
            )
        if plan:
            (root / ".ay" / "plans" / "task-1").mkdir(parents=True)
            (root / ".ay" / "plans" / "task-1" / "README.md").write_text("# plan", encoding="utf-8")
        if gate:
            (root / ".ay" / "tracking" / "gates" / "task-1-plan.gate").write_text(
                '{"task":"1","gate":"plan","since":"2026-08-15T14:00:00Z"}', encoding="utf-8"
            )
        if patched:
            cmds = root / ".claude" / "commands"
            cmds.mkdir(parents=True)
            (cmds / "go.md").write_text(
                'write .ay/tracking/gates/task-{N}-plan.gate on the plan gate', encoding="utf-8"
            )
        return Repo(name="tmp", path=root)

    def test_inferred_gate_for_unpatched_repo(self):
        gamma = next(r for r in self.cfg.repos if r.name == "gamma")
        inf = inferred_gates([gamma], NOW, self.WINDOW)
        self.assertEqual(len(inf), 1)
        self.assertEqual((inf[0].repo, inf[0].task, inf[0].source), ("gamma", "1", "inferred"))
        self.assertEqual(inf[0].gate_type, "inferred")

    def test_no_inference_when_gates_present(self):
        # A repo with a real .gate AND a stale lock+plan must NOT be inferred (patched wins).
        r = self._repo(lock_started="2026-08-15T14:00:00Z", plan=True, gate=True)
        self.assertEqual(inferred_gates([r], NOW, self.WINDOW), [])

    def test_patched_but_idle_repo_not_inferred(self):
        # Patched repo (go.md writes .gate) with NO live gate but a stale lock+plan must NOT
        # be inferred — its markers are authoritative; absence = nothing pending.
        r = self._repo(lock_started="2026-08-15T14:00:00Z", plan=True, patched=True)
        self.assertEqual(inferred_gates([r], NOW, self.WINDOW), [])

    def test_fresh_lock_not_inferred(self):
        # Lock started just before NOW (age < window) → not inferred.
        r = self._repo(lock_started="2026-08-15T15:19:00Z", plan=True)  # 60s < 600s
        self.assertEqual(inferred_gates([r], NOW, self.WINDOW), [])

    def test_lock_without_plan_not_inferred(self):
        r = self._repo(lock_started="2026-08-15T14:00:00Z", plan=False)
        self.assertEqual(inferred_gates([r], NOW, self.WINDOW), [])

    def test_real_gates_source_patched(self):
        for g in pending_gates(self.cfg.repos, NOW):
            self.assertEqual(g.source, "patched")


if __name__ == "__main__":
    unittest.main()
