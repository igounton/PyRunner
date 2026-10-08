# eval/ — the falsification test

The point of this repo is that your agents produce better output *with* the
mind than without it. That is a claim, and claims should be testable.

**Run an eval before you invest more in structure.** It is very easy to
build an elaborate knowledge base that makes output worse — longer,
blander, stuffed with retrieved context nobody needed.

## The protocol

1. **Collect ~15 real tasks.** Actual ones: posts you'd reply to, topics
   you'd write about, questions people ask you. Not invented prompts —
   invented prompts flatter the system.
2. **Generate two outputs per task, blind.** One with mind retrieval, one
   without, same model and instructions otherwise. Don't label them.
3. **Score each on three axes**, 1–5:
   - **Voice** — does it sound like you, or like an assistant?
   - **Substance** — does it contain something only you could say (a real
     number, a real failure, a specific take)?
   - **Would-I-post-it** — the only score that really matters.
4. **Read the losses.** Where the mind-backed version lost, ask which is
   true: the note was wrong, the note was missing, or retrieval picked the
   wrong notes. Each has a different fix.
5. **Fix the notes, not the harness.** The usual finding is that the notes
   are too abstract — summaries of positions rather than the positions
   themselves, with the verbatim quotes stripped out.

## Record results here

One file per run: `eval-YYYY-MM-<what-you-tested>.md`, with the tasks, the
scores, and what you changed afterwards. A dated trail of evals is how you
find out whether the mind is actually getting better or just bigger.

Files in this folder are infrastructure — never served as notes, never
retrieved as context.
