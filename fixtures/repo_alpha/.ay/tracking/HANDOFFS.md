# Agent Handoffs

## Schema (enforced)

Example (inside a fence — MUST be ignored by the parser):

```
---
To: someone
From: schema-example
Task: 99
Date: 2026-01-01 00:00
Summary: THIS IS A FENCED EXAMPLE AND MUST NOT APPEAR
Detail: if this shows up, fence-awareness is broken
---
```

---
To: all
From: claude
Task: 1
Date: 2026-08-16 09:00
Summary: Auth skeleton landed.
Detail: Sessions are cookie-based; see plan task-1.
---

---
To: all
From: claude
Task: 4
Date: 2026-08-16 10:00
Summary: Chain reader notes.
Detail: Commits come from BOARD.jsonl done events.
---
