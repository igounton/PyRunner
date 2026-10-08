---
contract-version: "1.1"
---

# YOUR MIND — Schema & Operating Contract

This repo is your brain: the single knowledge base all of your AI agents
read from for context, facts, and voice. This file is the contract. Every
agent that reads or writes this repo MUST follow it. If an instruction
elsewhere conflicts with this file, this file wins.

PyRun manages this brain as immutable database revisions. Its reader and feeder
prompts ship with the engine, outside this content tree. Every proposed feed
requires human approval. Workspace owners/admins may edit directly.

> **New here?** Read `README.md` first — it tells you which parts of this
> file to edit before you feed anything in. The structure below is the
> engine's contract and should stay as-is; the *taxonomy* (§7) and the
> identity files are yours to write.

---

## 1. Layout

| Path | Layer | What lives here |
|---|---|---|
| `INDEX.md` | Index | Generated per visibility tier from indexed entities. Read first, always. |
| `identity/` | Identity core | Who you are, how you write, what you believe. Small. Loaded by every consumer. |
| `projects/` | Cards | One card per project/product/series. Status, numbers, pitch, pointers. |
| `knowledge/` | Distilled notes | Atomic notes: takes, stories, lessons, facts. The retrieval workhorse. |
| `content-catalog/` | Inventory | Tables of published content per platform (for remix/repurpose queries). |
| `lenses/` | Scopes | Named default filters that consumers can invoke. |
| `raw/` | Archive | Full transcripts/posts, or pointers to them. Almost never loaded. |
| `eval/` | Quality | The falsification test. Run before trusting changes to the schema. |

## 2. Scope rule (hard)

**The mind knows ABOUT your work; it does not CONTAIN your work.**

A note is a distillation with provenance — a position, a story, a lesson, a
fact. The artefact itself (the codebase, the manuscript, the video file, the
course, the dataset) lives wherever it already lives. The project card
points at it.

Two consequences worth stating plainly:

- **Size**: if you find yourself pasting a whole document in, you want a
  `raw/` archive plus a note that links to it, not a giant note.
- **Language**: if some of your work is in another language, keep the mind
  in ONE language (whichever your agents write in) and let it hold the
  *about*-layer — what you are building, why, what you learned. A
  translated or mixed-language corpus inside the mind will poison voice
  retrieval. Point at the corpus from the project card instead.

## 3. Note frontmatter schema (knowledge/)

Every file in `knowledge/` starts with:

```yaml
---
id: take-2026-07-rag-vs-finetuning        # type-YYYY-MM-slug, unique
type: take | story | lesson | fact         # matches its folder
topics: [rag, agents]                      # from TAXONOMY below, 1-4 tags
projects: [my-library]                     # entities touched (optional)
source: yt-2026-06-rag-video                # id in content-catalog, or raw/ path
source_url: https://...                    # link to the original; null ONLY
                                           # for direct thoughts (see §5.2)
date: 2026-07                              # when this was said/published
description: One-line summary              # optional; used in the generated index
status: current | superseded
superseded_by: null                        # id of the newer note, if superseded
visibility: public | agents-only | private
---
```

Body rules:
- **Voice is sacred**: every `take` and `story` MUST include at least one
  verbatim quote of your actual words from the source, marked as
  `> VERBATIM: "..."`. Paraphrase around it, never instead of it.
- One idea per note. If extraction finds two ideas, write two notes.
- 5–15 lines of body. Notes are retrieval units, not essays.

Type meanings:
- `take` — an opinionated position ("my angle on X").
- `story` — a personal narrative with numbers/failures/outcomes.
- `lesson` — a transferable "what I learned building/testing X".
- `fact` — a stable, citable fact about your work or results.

## 4. Visibility (hard)

- `public` — derived from published content. Safe for any consumer,
  including future audience-facing chatbots.
- `agents-only` — derived from source code, planning docs, or unpublished
  thinking. Your own agents only. Any audience-facing serving layer MUST
  filter this out server-side, before context assembly.
- `private` — do not surface in any generated output; background context only.

Default: notes from published content → `public`. Notes from repos/planning →
`agents-only`. When unsure → `agents-only`.

### Path defaults (files with no `visibility:` frontmatter)

Explicit frontmatter always wins. Files without it resolve by path:

| Path | Resolution |
|---|---|
| `identity/*`, `knowledge/*`, `projects/*` | frontmatter required; missing → `agents-only` |
| `content-catalog/*` | `public` (inventory of published content) |
| `lenses/*` | `public` |
| `raw/*` | inherits the MAX visibility of the notes/cards linking to it; reachable only via those links, never by browsing |
| `INDEX.md` | serving layers serve a generated, tier-filtered index — never the raw file |
| `PENDING.md` | workbench — you + the feeder only; never served to any consumer tier |
| `eval/*`, `*/_TEMPLATE.md`, `CLAUDE.md`, `.claude/*`, `README.md` | infrastructure, not content — never served as notes |
| anything else unclassified | `private` (deny by default) |

### Line-level visibility

A `public` file may contain spans marked `(agents-only: ...)`. Serving
layers below agents-only tier MUST strip these spans — filtering whole
files is not enough.

## 5. Compiler rules (mind-feeder enforces these)

1. **Conservative extraction**: 2–4 strong notes per source. Depth lives in
   `raw/`; the note links to it.
2. **Provenance is mandatory**: no note without `source` + `source_url`.
   Exception: a source fed as a **direct thought** has no URL —
   `source_url: null` is valid there, and the feed id in `source` (plus the
   `raw/` archive of the thought) IS the provenance. Never invent a URL to
   satisfy this rule.
3. **Supersede, never delete**: a conflicting new claim marks the old note
   `status: superseded` + `superseded_by:`. Old note stays. If the conflict
   might be context-specific (both true in different scopes), keep BOTH
   current and note the boundary — do not force a winner.
4. **Dedupe conservatively**: when unsure whether two notes are duplicates,
   keep both and flag it. Prefer false duplicates over false merges.
5. **Never invent**: no claim that isn't in the source. No enrichment.
6. **Human write gate**: the feeder proposes; you approve; then it writes.
   Every approved feed is one atomic database revision.
7. **Freshness**: every project card touched by a feed gets
   `last-verified: YYYY-MM-DD` updated.

## 6. Reader rules (mind-reader enforces these)

1. Read `INDEX.md` first. Open only what the lens + task justify
   (typical budget: identity core + 2–5 files).
2. Respect `visibility`. Never quote `private`.
3. **Staleness**: if a project card's `last-verified` is older than 45 days,
   do not cite its specifics (numbers, status) as current — hedge or omit.
4. For voice tasks, prefer adapting VERBATIM quotes over summarizing notes.
5. `raw/` only when the task needs full depth (long-form writing, deep Q&A).
6. Superseded notes are history: never treat as current positions.

## 7. Topic taxonomy (controlled vocabulary)

The compiler picks tags ONLY from this list. Extend it deliberately (edit
this file in the same revision) — never invent ad-hoc tags. A tag is worth
adding when you have two or more notes that need it.

**Replace the list below with your own topics.** These are an example set
for a developer/creator; a researcher's or a founder's would look nothing
like it. Keep tags lowercase-with-hyphens, and keep the list short — a
taxonomy of 60 tags retrieves worse than one of 20.

`ai-agents, llm-engineering, rag, prompt-engineering, python, automation,
self-hosting, security, saas, build-in-public, content-creation, youtube,
seo, teaching, engineering-thinking, open-source, tools, entrepreneurship,
audience-growth`

## 8. Blocked scripts (optional)

Validator rule 6. If your brain is meant to hold one language and you
want the server to reject anything written in another script, list the
scripts here. **Leave this out and nothing is blocked** — that is the
default, and it is the right one for most brains.

The server recognises: `arabic`, `hebrew`, `cyrillic`, `greek`,
`devanagari`, `thai`, `han`, `hiragana`, `katakana`, `hangul`. A name it
doesn't recognise is reported as a warning on the approval screen rather
than silently ignored, because a typo here would quietly switch the rule
off.

This is coarse on purpose: it asks "does this file contain that script
at all", not "what language is this". A note that quotes one Arabic
phrase is rejected the same as one written entirely in Arabic.

To use it, uncomment and edit:

<!-- Scripts: `arabic` -->

## 9. Consumers

Anything can read this mind through `mind-reader` + a lens: reply agents,
newsletter/blog/video pipelines, remix tasks (an external proven
title/hook + your substance), niche-platform agents, and — via an MCP/API
door with server-side visibility filtering — audience-facing chatbots. The
repo structure never changes per consumer; only the lens does.

## 10. Contract version

The frontmatter at the top of this file carries `contract-version`.
Your brain is a copy of the template and never auto-updates, so this
records which contract your copy speaks. A server that ships a newer
contract will WARN — never refuse to boot — and may propose the upgrade
as a diff through the normal approval queue, where you review it like
any other write. Don't bump the version by hand: it changes only when
the template's contract itself changes.
