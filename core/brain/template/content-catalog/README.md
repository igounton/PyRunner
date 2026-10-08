# Content catalog

An inventory of what you have already published, one file per platform
(`youtube.md`, `blog.md`, `x.md`, `newsletter.md`…). Plain markdown tables.

This is not a knowledge layer — it holds no positions and no distillation.
It exists so an agent can answer two questions cheaply:

- **"Have I covered this already?"** — before writing something new.
- **"What can I repurpose?"** — for remix tasks, where you pair an
  externally-proven title/hook/format with your own substance.

Suggested columns (adapt freely):

| id | title | published | url | topics | notes |
|---|---|---|---|---|---|
| `yt-2026-07-some-video` | Some video | 2026-07-14 | https://… | rag, tools | `take-2026-07-...` |

The `id` column is what note frontmatter points at in its `source:` field,
so keep ids stable once they exist. Files here resolve to `public` by
default — it's a list of things you already published.
