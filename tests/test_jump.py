"""Tests for mlos.jump — cmux resolve/select strategy and the command-template strategy.

Runners are injected (no real process spawns). Signature: (argv, capture) -> stdout|None.
"""

import dataclasses
import json
import unittest
from pathlib import Path

from mlos.config import Repo, load_config
from mlos.jump import JumpError, _match_workspace_ref, build_focus_command, focus

FIXTURES = Path(__file__).parent.parent / "fixtures"


class Fake:
    """Records calls; returns canned stdout for the (capture=True) list call."""

    def __init__(self, list_json: str = "{}"):
        self.list_json = list_json
        self.calls: list = []

    def __call__(self, argv, capture):
        self.calls.append((argv, capture))
        return self.list_json if capture else None


class MatchTests(unittest.TestCase):
    def test_exact_match(self):
        ws = [{"current_directory": "/a/b", "ref": "workspace:2"}]
        self.assertEqual(_match_workspace_ref(ws, Path("/a/b")), "workspace:2")

    def test_subdir_match(self):
        ws = [{"current_directory": "/a/b/sub", "ref": "workspace:3"}]
        self.assertEqual(_match_workspace_ref(ws, Path("/a/b")), "workspace:3")

    def test_exact_preferred_over_subdir(self):
        ws = [
            {"current_directory": "/a/b/sub", "ref": "workspace:3"},
            {"current_directory": "/a/b", "ref": "workspace:9"},
        ]
        self.assertEqual(_match_workspace_ref(ws, Path("/a/b")), "workspace:9")

    def test_no_match(self):
        ws = [{"current_directory": "/x/y", "ref": "workspace:1"}]
        self.assertIsNone(_match_workspace_ref(ws, Path("/a/b")))


class CmuxStrategyTests(unittest.TestCase):
    def setUp(self):
        base = load_config(FIXTURES / "config.test.toml")
        self.cfg = dataclasses.replace(
            base, session_manager={"kind": "cmux", "focus_command": ["unused"]}
        )
        self.alpha = next(r for r in self.cfg.repos if r.name == "alpha")

    def test_selects_matching_workspace(self):
        payload = json.dumps(
            {"workspaces": [
                {"current_directory": "/somewhere/else", "ref": "workspace:1"},
                {"current_directory": str(self.alpha.path), "ref": "workspace:7"},
            ]}
        )
        fake = Fake(payload)
        focus(self.cfg, "alpha", run=fake)
        # first call lists (capture=True); second selects the matched ref (capture=False)
        self.assertEqual(fake.calls[0], (["cmux", "workspace", "list", "--json"], True))
        self.assertEqual(fake.calls[1], (["cmux", "workspace", "select", "workspace:7"], False))

    def test_fallback_opens_when_no_match(self):
        fake = Fake(json.dumps({"workspaces": [{"current_directory": "/nope", "ref": "workspace:1"}]}))
        focus(self.cfg, "alpha", run=fake)
        # no match → fall back to `cmux <path>`
        self.assertEqual(fake.calls[-1], (["cmux", str(self.alpha.path)], False))

    def test_bad_list_json_is_swallowed(self):
        fake = Fake("not json")
        focus(self.cfg, "alpha", run=fake)  # must not raise
        self.assertEqual(len(fake.calls), 1)  # listed, then gave up (no select/open)


class CommandStrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(FIXTURES / "config.test.toml")  # kind = "command", echo
        self.alpha = next(r for r in self.cfg.repos if r.name == "alpha")

    def test_build_focus_command(self):
        argv = build_focus_command(self.cfg, self.alpha)
        self.assertEqual(argv[:3], ["echo", "focus", "alpha"])
        self.assertTrue(argv[3].endswith("repo_alpha"))

    def test_command_invokes_runner(self):
        fake = Fake()
        focus(self.cfg, "alpha", run=fake)
        self.assertEqual(fake.calls[0][0][:3], ["echo", "focus", "alpha"])
        self.assertFalse(fake.calls[0][1])  # capture=False

    def test_argv_spaces_preserved(self):
        r = Repo(name="my repo", path=Path("/tmp/a b/c"))
        argv = build_focus_command(self.cfg, r)
        self.assertIn("my repo", argv)
        self.assertIn("/tmp/a b/c", argv)


class CommonTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(FIXTURES / "config.test.toml")

    def test_unknown_repo_raises_and_does_not_run(self):
        fake = Fake()
        with self.assertRaises(JumpError):
            focus(self.cfg, "nope", run=fake)
        self.assertEqual(fake.calls, [])

    def test_run_failure_is_swallowed(self):
        def boom(argv, capture):
            raise RuntimeError("cmux not running")

        focus(self.cfg, "alpha", run=boom)  # must not propagate
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
