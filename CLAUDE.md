# ml-os — Gate Inbox

## Read first

- **Architecture (the authority):** `docs/ARCHITECTURE.md`. No scope drift: changes
  that alter the design properties below need a doc change first, not just code.

## What this is

ml-os = **Gate Inbox**: a read-only local web viewer over the `.ay/` state of
registered ay-framework repos. One page: pending `/go` gates across all projects
(+ macOS notification), per-project boards, and the full trace chain
(task → plan → handoffs → commits). Dogfood build: this repo is built THROUGH
ay-framework and renders its own build chain.

- **Success measure:** how reliably gates get noticed — never "how much it renders".

## Architecture rules

- **The `.ay/` filesystem is the ONLY seam.** ml-os reads files from registered
  repos; it never writes into a watched repo, ever. No database; no state beyond
  config + notification bookkeeping.
- **Python stdlib + vanilla HTML/JS, no build step, zero dependencies.**
- **Config-driven:** all paths live in `config.toml` (tracked template:
  `config.example.toml`). A hardcoded path is a bug.
- **Render, don't aggregate:** no stats/charts/history.
- **Jump, don't act:** gate interaction = focusing the waiting session. Any write
  path toward a session is out of scope.

## Hard rules

- Read-only toward watched repos is a security property, not a preference — enforce
  it structurally (no write imports/helpers pointed at watched paths).
- The server default bind is `127.0.0.1`; never change that default. LAN exposure is
  a per-user opt-in in config.
- Tests run against fixture `.ay/` trees; validating UI changes means actually
  running the server and checking the page.
- No JS package manager. If JS/TS deps ever land, pin exact versions.

## Layout

- `docs/ARCHITECTURE.md` — design doc. `mlos/` — server + reader + notifier + jump.
- `fixtures/` — golden `.ay/` trees for tests. `config.example.toml` — the tracked
  config template (real `config.toml` is gitignored).
- `.ay/`, `.claude/` framework files — ay-framework operational state (gitignored,
  not part of the deliverable).
