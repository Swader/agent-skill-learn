---
name: agent-skill-learn
description: Create, maintain, and run spaced repetition learning decks for users. Use when the user asks to learn, memorize, review, quiz, add material to cards, import notes/posts/docs into cards, update outdated cards, schedule recall, run a daily review, grade answers, or build a lightweight SQLite spaced repetition store with JSON import/export.
---

# Agent Skill Learn

## Default Behavior

Turn durable knowledge into short cards, then run reviews one question at a
time. Use SQLite as the review-history store. Use JSON for card batches and
exports, not as the scheduling source of truth.

Use `scripts/sr_tool.py` for deterministic deck operations:

```bash
python3 scripts/sr_tool.py where
python3 scripts/sr_tool.py init
python3 scripts/sr_tool.py add-json --cards ./cards.json
python3 scripts/sr_tool.py due --limit 10
python3 scripts/sr_tool.py reveal --card-id CARD
python3 scripts/sr_tool.py record-review --card-id CARD --grade 4 --answer-file answer.txt
python3 scripts/sr_tool.py export-json --out ./cards_export.json
```

## Deck Resolution

Treat learning as user-level state, not project-level state. If the user has an
existing learn deck, append to it from any project instead of starting a new
deck.

Resolve the deck path in this order:

1. Explicit `--db`.
2. Environment variables: `AGENT_SKILL_LEARN_DB`, `LEARN_DB`, `SR_DB_PATH`.
3. Harness env files when present: `~/.config/agent-skill-learn/.env`,
   `~/.codex/local.env`, `~/.config/codex/local.env`, `~/.claude/.env`,
   `~/.config/claude/local.env`, `~/.cursor/.env`, and
   `~/.config/cursor/local.env`.
4. User config: `~/.config/agent-skill-learn/config.json`.
5. Existing project deck at `./sr/cards.sqlite`, only if it already exists.
6. Default user deck: `${XDG_DATA_HOME:-~/.local/share}/agent-skill-learn/cards.sqlite`.

Run `python3 scripts/sr_tool.py where` before adding cards if the active deck is
unclear. To bind an existing deck as the user default, run:

```bash
python3 scripts/sr_tool.py configure --db /path/to/cards.sqlite
```

Do not create a fresh project-local deck unless the user explicitly asks for a
project-scoped deck or passes `--db ./sr/cards.sqlite`.

## Adding Material

When the user says something like `/learn add this post to cards`:

1. Identify what should become durable recall. Skip filler, opinions without a
   reusable lesson, and details the user can easily look up.
2. Split into atomic cards. One card should test one fact, distinction, warning,
   workflow step, or mental model.
3. Prefer basic Q/A cards. Use cloze cards only when the exact missing term,
   number, command, or invariant matters.
4. Include source, tags, and priority.
5. Search the existing deck for duplicates or outdated cards before adding.
6. Update or supersede old cards when new content replaces them. Do not leave
   contradictory active cards.
7. Run `sr_tool.py stats` and, when useful, `sr_tool.py export-json` after edits.

Read `references/card_quality.md` when converting a large source or when cards
feel vague.

## Card Shape

Use this JSON shape when preparing card batches:

```json
[
  {
    "id": "stable-id",
    "type": "basic",
    "front": "What should the learner recall?",
    "back": "The concise answer, with one useful why/context sentence.",
    "tags": ["topic", "source"],
    "source": "path, URL, meeting, or note",
    "priority": "P1",
    "supersedes": []
  }
]
```

Priorities:

- `P0`: must know now; daily until stable.
- `P1`: core working knowledge.
- `P2`: useful context.
- `P3`: low-frequency background.

Stable ids should survive rebuilds. Use a domain prefix plus a short slug:
`slack-ea-token-owner`, `person-jonathan-vocab`, `pricing-99-signal`.

## Review Sessions

Run reviews as a tight loop:

1. Select due cards, default 10. `due` omits answers by default so the review
   loop does not leak the back side.
2. Ask exactly one question.
3. Wait for the user's answer.
4. Reveal the correct answer after every response with `reveal --card-id`.
5. If the user answers "I don't know", "don't know", "idk", or a close
   equivalent, reveal the answer, give one short memory hook for how to remember
   it, and record recall as `0` without debating the grade.
6. Estimate recall grade from any other answer, then ask the user to override if they
   disagree.
7. Record the review and next due date.
8. Ask the next due question, or offer more only after the requested count.

Recall grading:

- `5`: exact, confident, includes the important nuance.
- `4`: correct with minor missing detail.
- `3`: mostly right but needed effort or missed a secondary nuance.
- `2`: recognized the topic but got a material part wrong.
- `1`: vague familiarity only.
- `0`: no recall or wrong concept.

For grades below `3`, reset the interval to one day. For `3+`, use the SM-2
style schedule implemented by `sr_tool.py`.

## Updating Outdated Cards

When new material contradicts old cards:

- Prefer editing the old card if the learning target is the same.
- Supersede and deactivate the old card if the mental model changed.
- Keep a revision trail in the deck.
- Explain the replacement in plain language if the user will care.

Do not silently add near-duplicates. The user should not rehearse stale facts.

## Output Style

During review, keep the transcript compact:

```text
Card 3/10
Question: ...

Correct answer: ...
Memory hook: ...
Grade: 4. Next due: 2026-06-01.
```

When adding cards, report count added, count updated/superseded, and any source
material intentionally skipped.
