<p align="center">
  <img src="docs/logo.svg" width="120" alt="ml-os logo"/>
</p>

<h1 align="center">ml-os</h1>

<p align="center"><strong>A read-only inbox for the moments your AI agents wait on you.</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.0-7c3aed?style=flat-square" alt="Version 0.1.0"/>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT License"/></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/dependencies-zero-3ecf8e?style=flat-square" alt="Zero dependencies"/>
  <img src="https://img.shields.io/badge/notifications-macOS-f5b043?style=flat-square" alt="macOS notifications"/>
</p>

## Why this exists

My agents run autonomously except for exactly two moments per task: approving the plan and approving the code. Those approvals wait silently inside terminal sessions. With a few projects running at once, an agent would sit blocked for hours before I noticed. The agents were fine. I was the bottleneck.

ml-os is one local page that answers a single question: what is waiting on me right now, across every project? It shows pending [ay-framework](https://github.com/liwala/ay-framework) `/go` gates oldest first, escalates the stale ones, fires a macOS notification the second a gate flips to pending, and jumps you straight into the session that is waiting.

![The Gate Inbox](docs/screenshot.png)

## What you get

- Every pending gate across your registered repos, oldest first, with task titles and live waiting times
- One notification per gate transition, and clicking it opens the inbox
- Click a gate and the waiting session gets focused (cmux out of the box, or any command you configure)
- Each repo's task board and blockers, rendered from its `.ay/` tracking files
- A walkable trace per task: plan, then handoffs, then the commits that landed
- Repos without the gate marker still surface an "inferred" gate when a task lock looks stuck, clearly labeled as a guess
- No database and no build step. The only runtime is the Python standard library plus one HTML file
- ml-os never writes into a watched repo. Read-only is structural: the code has no write path toward a watched folder

## Quickstart

You need Python 3.11+. Notifications need macOS and `brew install terminal-notifier`; the viewer itself runs anywhere.

```bash
git clone https://github.com/MuLIAICHI/ml-os.git
cd ml-os
cp config.example.toml config.toml   # register your repos in here
python3 -m mlos.server
```

Open http://localhost:8787. The tab title carries the pending-gate count, so a pinned tab doubles as a status light.

## Configuration

Everything lives in `config.toml`, and the annotated template is `config.example.toml`. Registering a project takes one block:

```toml
[[repos]]
name = "my-project"
path = "~/code/my-project"
```

A note on exposure: the server is unauthenticated and `/api/jump` runs a command you configure, so it binds `127.0.0.1` by default. Bind `0.0.0.0` only on a network where you trust every device. That is the intended mode for checking gates from your phone, and it stays opt-in.

## How it works

ml-os reads `<repo>/.ay/tracking/gates/*.gate` markers. A marker on disk means a `/go` cycle is waiting on a human. Its JSON carries the gate type (`plan` or `code`) and a since-timestamp, and the repo's `/go` deletes the marker once you respond. Boards, blockers, plans and commit logs come from the same `.ay/` tree. That filesystem is the only seam between ml-os and your repos, which is also what makes the whole app testable against golden fixtures.

The full design lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

The suite runs against golden fixture `.ay/` trees in `fixtures/`, with injected clocks where timing matters. No live agents involved, and results are deterministic.

## Dogfood proof

ml-os was built through ay-framework itself. It is registered in its own config and renders its own build chain, meaning the tasks, plans, handoffs and commits that produced it.

## License

[MIT](LICENSE)
