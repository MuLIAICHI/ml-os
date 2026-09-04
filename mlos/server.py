"""ml-os HTTP server — stdlib only, no framework, no build step.

Serves two things:
  * ``GET /``           the single vanilla HTML page
  * ``GET /api/state``  JSON snapshot of pending gates across all registered repos

Bound to ``0.0.0.0`` by default so a phone on the same LAN can reach the viewer.
The server is read-only toward watched repos: it only ever calls :mod:`mlos.reader`,
which never writes into a repo.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mlos.config import Config, load_config
from mlos.jump import JumpError, focus
from mlos.notifier import Notifier
from mlos.reader import inferred_gates, pending_gates, project_boards, task_chain

MAX_BODY_BYTES = 64 * 1024  # cap POST bodies (jump takes a tiny JSON object)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"


def build_state(config: Config, now: datetime) -> dict:
    """Assemble the ``/api/state`` payload for a given reference time.

    Args:
        config: Loaded configuration.
        now: Reference time (injected for determinism in tests).

    Returns:
        A JSON-serializable dict ``{"gates": [...]}`` with gates oldest-first, each
        carrying a computed ``stale`` flag (age beyond the configured threshold).
    """
    threshold = config.stale_threshold_seconds
    # Real (patched) gates + inferred gates for unpatched repos (task 6), oldest-first.
    gates = pending_gates(config.repos, now)
    gates = gates + inferred_gates(config.repos, now, config.inference_window_seconds)
    gates.sort(key=lambda g: g.age_seconds, reverse=True)
    boards = project_boards(config.repos)
    return {
        "gates": [
            {
                "repo": g.repo,
                "task": g.task,
                "gate_type": g.gate_type,
                "since": g.since_canonical,
                "age_seconds": round(g.age_seconds, 1),
                "stale": g.age_seconds > threshold,
                "source": g.source,
            }
            for g in gates
        ],
        "boards": [
            {
                "repo": b.repo,
                "initialized": b.initialized,
                "tasks": [
                    {
                        "num": t.num,
                        "title": t.title,
                        "agent": t.agent,
                        "status": t.status,
                        "status_keyword": t.status_keyword,
                        "blocked_by": t.blocked_by,
                    }
                    for t in b.tasks
                ],
                "blockers": [
                    {
                        "id": bl.id,
                        "description": bl.description,
                        "task": bl.task,
                        "owner": bl.owner,
                        "status": bl.status,
                        "since": bl.since,
                    }
                    for bl in b.blockers
                ],
            }
            for b in boards
        ],
    }


def chain_payload(chain) -> dict:
    """JSON-serialize a :class:`~mlos.reader.TaskChain` (``from_`` -> ``from``)."""
    return {
        "repo": chain.repo,
        "task": chain.task,
        "plan_files": [{"name": f.name, "content": f.content} for f in chain.plan_files],
        "handoffs": [
            {
                "to": h.to, "from": h.from_, "task": h.task,
                "date": h.date, "summary": h.summary, "detail": h.detail,
            }
            for h in chain.handoffs
        ],
        "commits": [{"ts": c.ts, "commit": c.commit, "title": c.title} for c in chain.commits],
    }


class _Handler(BaseHTTPRequestHandler):
    """Request handler bound to a :class:`~mlos.config.Config` via ``server.config``."""

    # Set on the server instance in serve(); typed here for clarity.
    server_version = "ml-os/0.1"

    @property
    def config(self) -> Config:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        """Route access logs through logging instead of stderr prints (rule 10)."""
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        """Serve the page, the state API, the task-chain API, or a 404."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._serve_index()
        elif path == "/api/state":
            self._serve_state()
        elif path == "/api/task":
            self._serve_task(parse_qs(parsed.query))
        else:
            self._send(404, "text/plain; charset=utf-8", b"404 not found")

    def _serve_index(self) -> None:
        try:
            body = INDEX_FILE.read_bytes()
        except OSError as exc:
            log.error("cannot read index.html: %s", exc)
            self._send(500, "text/plain; charset=utf-8", b"500 index unavailable")
            return
        self._send(200, "text/html; charset=utf-8", body)

    def _serve_state(self) -> None:
        state = build_state(self.config, datetime.now(timezone.utc))
        body = json.dumps(state).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body)

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        """Handle POST endpoints (currently only /api/jump)."""
        path = urlparse(self.path).path
        if path == "/api/jump":
            self._serve_jump()
        else:
            self._send(404, "text/plain; charset=utf-8", b"404 not found")

    def _read_json_body(self) -> dict | None:
        """Read and parse a small JSON request body; None if absent/too large/invalid."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _serve_jump(self) -> None:
        """Focus the session for the posted repo (read-only + jump; never injects input)."""
        body = self._read_json_body()
        repo_name = (body or {}).get("repo")
        if not repo_name:
            self._send(400, "text/plain; charset=utf-8", b"400 missing repo")
            return
        try:
            focus(self.config, str(repo_name))
        except JumpError:
            self._send(404, "text/plain; charset=utf-8", b"404 unknown repo")
            return
        self._send(200, "application/json; charset=utf-8", b'{"ok":true}')

    def _serve_task(self, query: dict) -> None:
        """Serve the trace chain for ``?repo=<name>&task=<N>`` (404 on unknown repo)."""
        repo_name = (query.get("repo") or [""])[0]
        task = (query.get("task") or [""])[0]
        if not repo_name or not task:
            self._send(400, "text/plain; charset=utf-8", b"400 missing repo or task")
            return
        repo = next((r for r in self.config.repos if r.name == repo_name), None)
        if repo is None:
            self._send(404, "text/plain; charset=utf-8", b"404 unknown repo")
            return
        body = json.dumps(chain_payload(task_chain(repo, task))).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body)


def make_server(config: Config) -> ThreadingHTTPServer:
    """Create (but do not start) a threaded HTTP server bound per ``config``.

    Threaded so concurrent ~5s polls from several clients (desktop + phone) don't
    block one another. A port of 0 binds an ephemeral port (used by tests).
    """
    httpd = ThreadingHTTPServer((config.host, config.port), _Handler)
    httpd.config = config  # type: ignore[attr-defined]
    return httpd


def start_watcher(config: Config, notifier: Notifier) -> threading.Event:
    """Start a daemon thread that polls pending gates and notifies on new ones.

    Returns a :class:`threading.Event` — set it to stop the loop. The loop uses
    ``stop.wait(interval)`` as an interruptible sleep, and swallows-and-logs any tick
    error so a transient read failure never kills the watcher.
    """
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(config.poll_interval_seconds):
            try:
                gates = pending_gates(config.repos, datetime.now(timezone.utc))
                notifier.sync(gates)
            except Exception as exc:  # a bad tick must not kill the watcher
                log.error("watcher tick failed: %s", exc)

    threading.Thread(target=loop, name="ml-os-watcher", daemon=True).start()
    return stop


def serve(config: Config) -> None:
    """Run the server forever (with the notification watcher), logging the reachable URL."""
    httpd = make_server(config)
    notifier = Notifier(
        config.notifier.state_file,
        backend=config.notifier.backend,
        open_url=config.notifier.open_url,
    )
    # Silent seed on a true first run so launch doesn't burst a notification per
    # already-pending gate; only post-launch transitions fire.
    initial = pending_gates(config.repos, datetime.now(timezone.utc))
    if not notifier.had_state:
        notifier.prime(initial)
    stop = start_watcher(config, notifier)

    host, port = httpd.server_address[0], httpd.server_address[1]
    # Operator-facing banner: acceptable print at the entrypoint (rule 10).
    print(f"ml-os serving on http://{host}:{port}  (LAN-reachable if host is 0.0.0.0)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nml-os stopped.")
    finally:
        stop.set()
        httpd.server_close()


def main() -> None:
    """Entrypoint: load ``config.toml`` from the repo root and serve."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(Path(__file__).parent.parent / "config.toml")
    serve(config)


if __name__ == "__main__":
    main()
