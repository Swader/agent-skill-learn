# agent-skill-learn

`agent-skill-learn` is a Codex skill for turning notes, posts, meeting transcripts, docs, and code discoveries into spaced repetition cards.

It is built for the workflow where you tell an agent:

```text
/learn add this post to cards
```

The agent should pull out the useful pieces, avoid duplicates, replace stale cards, and leave you with a deck you can actually review. Not a pile of trivia. Not a summary pretending to be memory.

## What it does

- Creates or updates a lightweight SQLite spaced repetition deck.
- Adds cards from source material with tags, priority, source, and stable ids.
- Detects near-duplicate cards by semantic key and can deactivate older versions.
- Records reviews with 0-5 recall grades.
- Schedules cards with a simple SM-2 style interval rule.
- Imports and exports JSON when you need to inspect or move card batches.
- Resolves one user-level default deck so cards can be added from any project.

## Layout

```text
skills/agent-skill-learn/
  SKILL.md
  agents/openai.yaml
  scripts/sr_tool.py
  references/card_quality.md
```

The repo root has this README for humans. The skill itself lives in `skills/agent-skill-learn/`.

## Quick start

```bash
python3 skills/agent-skill-learn/scripts/sr_tool.py configure --db /path/to/cards.sqlite
python3 skills/agent-skill-learn/scripts/sr_tool.py where
python3 skills/agent-skill-learn/scripts/sr_tool.py init
python3 skills/agent-skill-learn/scripts/sr_tool.py add-json --cards ./cards.json
python3 skills/agent-skill-learn/scripts/sr_tool.py due --limit 10
python3 skills/agent-skill-learn/scripts/sr_tool.py reveal --card-id slack-ea-token-owner
```

To install it for Codex, symlink or copy `skills/agent-skill-learn/` into your Codex skills directory.

## Deck location

The deck is meant to be personal, not tied to a checkout. If you omit `--db`,
`sr_tool.py` resolves the path from:

1. `AGENT_SKILL_LEARN_DB`, `LEARN_DB`, or `SR_DB_PATH`
2. common harness env files such as `~/.codex/local.env`, `~/.claude/.env`, and `~/.cursor/.env`
3. `~/.config/agent-skill-learn/config.json`
4. an existing `./sr/cards.sqlite`
5. `${XDG_DATA_HOME:-~/.local/share}/agent-skill-learn/cards.sqlite`

Use `where` to see the active deck before adding cards from an unrelated
project.

## Card format

```json
[
  {
    "id": "slack-ea-token-owner",
    "type": "basic",
    "front": "Who owns Slack EA token refresh in shared app mode?",
    "back": "Legacy Slack auth owns shared token material. Slack EA may borrow the bot token but must not refresh or revoke it.",
    "tags": ["slack-ea", "tokens"],
    "source": "slack-task/how-slack-events-actually-flow.md",
    "priority": "P1"
  }
]
```

## Review loop

Daily default:

1. Ask 10 due cards.
2. Wait for the answer.
3. Show the correct answer.
4. If the answer is "I don't know" or equivalent, give a short memory hook and
   record recall as 0.
5. Grade other answers from 0 to 5.
6. Record the review and next due date.

The `due` command does not print answers unless you explicitly pass
`--include-back`. The skill asks one question at a time because that is the only
review loop that tends to survive real life.
