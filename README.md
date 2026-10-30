# Code Guard

**Any AI assistant reads this folder and starts shipping engineering-grade code by default** — no setup, no config, works in Doubao / ChatGPT / Claude / Gemini / Codex / Cursor.

It does two jobs at once:

1. **Upstream** — a default "human-engineer" standard that every AI must follow *while writing code*, so output comes out clean on the first pass (meaningful names, comments that explain *why*, real error handling, sane structure, README + build + `.gitignore` included, honest incremental git history).
2. **Downstream** — a detector + cleanup toolkit for existing code: was this written by an AI? where is the dead code? what should I clean before open-sourcing it? how do I de-AI-flavor it without changing behavior?

> No detector is 100%. We output **evidence + confidence**, never a bare verdict. Code AI wrote and a human edited a few lines of is usually undecidable — we say so.

---

## Why

AI code looks *cleaner* than human code in a suspicious way:

- every variable named `data` / `result` / `temp`
- comments that explain *what* instead of *why*
- perfect little functions, uniform style, zero debug leftovers, one giant initial commit
- error handling that catches everything and logs nothing specific

Code Guard makes your AI **stop doing that by default**, and gives you a scanner to catch it anyway.

## Quick start (30 seconds)

Drop this folder into your project (or any agent's skills directory). That's it.

```
your-project/
└── code-guard/        ← this folder
    ├── SKILL.md       ← the AI reads this
    ├── scripts/       ← stdlib-only Python, no pip install
    └── references/
```

Then just ask your AI to write code. It will:

> `[code-guard] Engineering-grade output: meaningful names / comments explain why / real error handling / project files included`

and ship accordingly. No prompt engineering, no per-chat reminder.

## Use it as a scanner

```bash
# Objective signals from a repo / directory → JSON
python scripts/analyze_repo.py ./my-project

# Humanize-flavor checklist → Markdown
python scripts/humanize.py ./my-project

# Structured checklist for other tools to consume
python scripts/humanize.py ./my-project --format json
```

Stdlib only. Python 3. Git optional (it just enriches the report).

## The five workflows

| Workflow | Trigger | What it does |
| --- | --- | --- |
| **E — Default writing standard** | *Any* code writing task, automatically | Naming, comments, error handling, structure, project files, honest git history |
| **A — AI-origin detection** | "is this code AI-written?" | Objective metrics + signal checklist → evidence + confidence report |
| **B — Dead-code scan** | "find dead code" | Unused imports, reassigned vars, stubs, placeholder returns, dead comments (candidates only — you verify) |
| **C — Pre-OSS cleanup** | "clean this up before open sourcing" | Teaching comments, generic names, debug leftovers, dead code, README/LICENSE/.gitignore |
| **D — Humanize refactor** | "make this look hand-written" | Refactor appearance only — **behavior unchanged**, honest incremental commits |

## The "say it out loud" rule

Any AI that loads this skill **announces it once** at the start of a coding task:

```
[code-guard] Engineering-grade output: meaningful names / comments explain why / real error handling / project files included
```

So you — and every other AI reading the session — can see at a glance that the code already follows the standard and doesn't need another review pass.

## Honest red lines

Code Guard does **real engineering**, not deception:

- ❌ No fabricated git history, no backdated commits, no fake "developed over months" story
- ❌ No impersonation of a real person, no style-mimicry against detectors
- ❌ No behavior change — refactors are appearance-only and must recompile/re-test after
- ✅ Anonymous/pen-name authorship for public repos is fine (that's privacy, not fakery)
- ✅ "We can't tell" is a first-class answer

## Requirements

- Python 3 (scripts use the standard library only)
- Git optional — commit history metrics and real incremental commits when present

## License

[MIT](LICENSE). Use it in any AI toolchain, fork it, ship it.

---

If this saves you a review pass, a ⭐ is appreciated.
