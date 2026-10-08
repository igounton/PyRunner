---
id: project-example-project
kind: project
topics: [tools, open-source]
visibility: public
last-verified: 2026-01-01
links:
  site: https://example.com
  repo: https://github.com/you/example-project
  docs:
---
# Example Project

> **DELETE THIS FILE.** It exists so a fresh clone has one indexable entity
> and you can see a card rendered in the brain browser before you've
> written anything. Copy `_TEMPLATE.md` for your real projects.

## What it is (elevator pitch)
A small open-source tool that does one thing well. Two or three sentences,
written the way you'd say it out loud — this is the text an agent will
paraphrase when someone asks what you're building.

## Current status
Shipped and maintained. Note that `last-verified` above is deliberately
old, so this card shows up as **stale** on the dashboard — that's the
freshness rule working, not a bug.

## Key numbers
- 120 GitHub stars (2026-01)
- ~40 weekly installs (2026-01)

Every number carries the month it was true. Readers hedge or omit numbers
from a card that hasn't been verified in 45 days.

## Architecture / how it works
One CLI entry point, a small core library, no service dependencies.
(agents-only: this span is stripped for public-tier consumers — use it for
detail you'd share with your own agents but not with an audience bot.)

## Decisions & constraints
Chose a single binary over a plugin system: fewer moving parts matter more
than extensibility at this size.

## Pointers
- Deep docs / planning: link to wherever they really live
- Related notes: none yet
