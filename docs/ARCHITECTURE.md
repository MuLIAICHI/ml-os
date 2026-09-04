# ml-os — architecture

ml-os (the **Gate Inbox**) is a read-only local web viewer over the `.ay/` state of
repos built with [ay-framework](https://github.com/liwala/ay-framework). One page
answers one question: **what is waiting on me right now, across all projects?**

In an ay-framework `/go` cycle, everything is autonomous except two human approvals
per task: the plan and the code. Those approvals wait silently inside a terminal
session. Run several projects at once and a cycle can sit blocked for hours because
nothing tells you a gate is pending. ml-os surfaces those gates and gets you to them.

## Design properties

1. **The `.ay/` filesystem is the only seam.** ml-os reads files from registered
   repos; it never writes into a watched repo. There is no database and no state
   beyond the config file and the notifier's own bookkeeping file. The read-only
   property is structural: the notifier is never handed a repo path, and no module
   that touches watched paths imports a write helper.
2. **Render, don't aggregate.** No stats, charts, or history. The page renders
   current state only.
3. **Jump, don't act.** Clicking a gate focuses the waiting session (via the
   configured session manager). Approving still happens in the session. There is no
   write path from the browser toward an agent.
4. **Zero dependencies.** Python stdlib only (`http.server`, `tomllib`, `unittest`)
   plus vanilla HTML/JS with no build step.
5. **Config-driven.** All paths live in `config.toml`. A hardcoded path anywhere is
   a bug.

## Components

| Piece | Role |
|---|---|
| `mlos/config.py` | Load and validate the single TOML config (fail loud, never default silently) |
| `mlos/reader.py` | Read gates, boards, blockers, and trace chains from `.ay/` trees |
| `mlos/notifier.py` | Fire-once-per-transition macOS notifications; owns its own state file |
| `mlos/jump.py` | Focus the waiting session (cmux workspace resolution or a configured command) |
| `mlos/server.py` | Threaded stdlib HTTP server: the page, `/api/state`, `/api/task`, `/api/jump` |
| `mlos/static/index.html` | The whole UI — one self-contained file, no build step |
| `fixtures/` | Golden `.ay/` trees the test suite runs against |

## The gate signal

`/go` gates historically waited in-session with no disk marker. Each registered
repo's `go.md` gets a small instruction patch: on presenting a gate, write
`.ay/tracking/gates/{task}.gate` (JSON: task id, gate type, timestamp); on receiving
the human's response, delete it. The viewer treats the presence of a `.gate` file as
"pending". For repos without the patch, ml-os falls back to **inference**: a task
lock held longer than a configurable window with no recent commits renders as an
"inferred" gate, clearly marked uncertain.

## Data flow

```
watched repos (.ay/ trees)          ml-os                        you
  gates/*.gate  ─────────►  reader (poll ~5s) ──► /api/state ──► browser page
  BOARD.md, BLOCKERS.md ──►     │                                   │ click gate
  plans/, HANDOFFS.md ────►     ├──► notifier ──► macOS banner ─────┤ click banner
                                └──► jump ◄──── /api/jump ◄─────────┘
                                      └──► session manager focuses the waiting session
```

## Security posture

The server is unauthenticated and `/api/jump` executes a configured local command,
so the default bind is `127.0.0.1`. Binding `0.0.0.0` (e.g. to check gates from a
phone) is an explicit opt-in documented in `config.example.toml`; only do it on a
network where you trust every device.

## Testing

`python3 -m unittest discover -s tests` runs against the golden fixture trees in
`fixtures/` — no network, no real repos, injected clocks and senders where
determinism matters.
