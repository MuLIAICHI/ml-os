"""Tests for mlos.server — state assembly and the HTTP surface.

``build_state`` is tested with an injected ``now`` (deterministic). The HTTP routes are
tested against a real loopback server on an ephemeral port; those assertions check
structure and status codes, not wall-clock ages.
"""

import json
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mlos.config import load_config
from mlos.server import build_state, make_server

FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 8, 15, 15, 20, 0, tzinfo=timezone.utc)


class BuildStateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(FIXTURES / "config.test.toml")

    def test_state_shape_and_count(self):
        state = build_state(self.cfg, NOW)
        self.assertIn("gates", state)
        # 3 real (patched) gates + 1 inferred (gamma: lock+plan, no .gate) = 4
        self.assertEqual(len(state["gates"]), 4)
        first = state["gates"][0]
        self.assertEqual(
            set(first),
            {"repo", "task", "gate_type", "since", "age_seconds", "stale", "source"},
        )

    def test_stale_flag_computed(self):
        by_task = {g["task"]: g for g in build_state(self.cfg, NOW)["gates"]}
        self.assertTrue(by_task["2"]["stale"])   # 8400s > 1800s
        self.assertFalse(by_task["1"]["stale"])  # 600s < 1800s

    def test_oldest_first(self):
        gates = build_state(self.cfg, NOW)["gates"]
        self.assertEqual(gates[0]["task"], "2")

    def test_state_includes_inferred_gate(self):
        gates = build_state(self.cfg, NOW)["gates"]
        sources = {(g["repo"], g["task"]): g["source"] for g in gates}
        # gamma (unpatched: lock+plan, no .gate) is inferred; real gates are patched.
        self.assertEqual(sources.get(("gamma", "1")), "inferred")
        self.assertEqual(sources.get(("alpha", "1")), "patched")

    def test_state_has_boards(self):
        state = build_state(self.cfg, NOW)
        self.assertIn("boards", state)
        by_repo = {b["repo"]: b for b in state["boards"]}
        # one board entry per registered repo
        self.assertEqual(set(by_repo), {"alpha", "beta", "gamma", "delta"})
        self.assertTrue(by_repo["alpha"]["initialized"])
        self.assertEqual(len(by_repo["alpha"]["tasks"]), 4)
        self.assertEqual(len(by_repo["alpha"]["blockers"]), 1)
        self.assertFalse(by_repo["delta"]["initialized"])
        # task rows carry the normalized status keyword for the UI
        t1 = next(t for t in by_repo["alpha"]["tasks"] if t["num"] == "1")
        self.assertEqual(t1["status_keyword"], "DONE")


class HttpSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = load_config(FIXTURES / "config.test.toml")
        cls.httpd = make_server(cfg)  # host 127.0.0.1, port 0 → ephemeral
        cls.host, cls.port = cls.httpd.server_address[0], cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def test_api_state_returns_gates(self):
        with urllib.request.urlopen(self._url("/api/state"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
        self.assertEqual(len(data["gates"]), 4)  # 3 patched + 1 inferred (gamma)

    def test_root_returns_html(self):
        with urllib.request.urlopen(self._url("/"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            body = resp.read().decode("utf-8")
        self.assertIn("Gate Inbox", body)

    def test_unknown_path_404(self):
        try:
            urllib.request.urlopen(self._url("/nope"), timeout=5)
            self.fail("expected HTTP 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_api_task_returns_chain(self):
        with urllib.request.urlopen(self._url("/api/task?repo=alpha&task=1"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
        self.assertEqual(data["repo"], "alpha")
        self.assertEqual(len(data["plan_files"]), 2)
        self.assertEqual(len(data["handoffs"]), 1)
        self.assertEqual(data["handoffs"][0]["from"], "claude")  # from_ -> "from"
        self.assertEqual([c["commit"] for c in data["commits"]], ["aaa1111"])

    def test_api_task_unknown_repo(self):
        try:
            urllib.request.urlopen(self._url("/api/task?repo=nope&task=1"), timeout=5)
            self.fail("expected HTTP 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def _post(self, path: str, payload: dict):
        req = urllib.request.Request(
            self._url(path), data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return urllib.request.urlopen(req, timeout=5)

    def test_api_jump_ok(self):
        # fixture focus_command is a harmless echo; a real subprocess runs but does nothing.
        with self._post("/api/jump", {"repo": "alpha"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read()), {"ok": True})

    def test_api_jump_unknown_repo(self):
        try:
            self._post("/api/jump", {"repo": "nope"})
            self.fail("expected HTTP 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
