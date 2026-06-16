#!/usr/bin/env python3
"""SQLite spaced repetition helper for agent-skill-learn."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_tags(tags) -> list[str]:
    if isinstance(tags, str):
        parts = re.split(r"[, ]+", tags)
    else:
        parts = tags or []
    return sorted({str(part).strip().lower() for part in parts if str(part).strip()})


def normalize_priority(priority) -> str:
    value = str(priority or "P2").strip().upper()
    if value not in {"P0", "P1", "P2", "P3"}:
        raise SystemExit(f"Invalid priority {priority!r}; use P0, P1, P2, or P3.")
    return value


def semantic_key(front: str) -> str:
    text = re.sub(r"`([^`]+)`", lambda match: f" {match.group(1)} ", front.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    stop = {"what", "why", "how", "does", "do", "the", "a", "an", "is", "are", "to", "for", "of", "in", "and"}
    words = [word for word in text.split() if word not in stop]
    return "-".join(words[:12]) or hashlib.sha1(front.encode()).hexdigest()[:12]


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def xdg_config_dir() -> Path:
    return expand_path(os.environ.get("XDG_CONFIG_HOME", "~/.config")) / "agent-skill-learn"


def xdg_data_dir() -> Path:
    return expand_path(os.environ.get("XDG_DATA_HOME", "~/.local/share")) / "agent-skill-learn"


DB_ENV_KEYS = ("AGENT_SKILL_LEARN_DB", "LEARN_DB", "SR_DB_PATH")
SEMANTIC_KEY_VERSION = "2"
CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::(.*?))?\}\}")


def prompt_front(front: str) -> str:
    return CLOZE_RE.sub(lambda match: match.group(2) or "____", front)


def harness_env_files() -> list[Path]:
    return [
        xdg_config_dir() / ".env",
        expand_path("~/.codex/local.env"),
        expand_path("~/.config/codex/local.env"),
        expand_path("~/.claude/.env"),
        expand_path("~/.config/claude/local.env"),
        expand_path("~/.cursor/.env"),
        expand_path("~/.config/cursor/local.env"),
    ]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key in DB_ENV_KEYS and value:
            values[key] = value
    return values


def config_json_path() -> Path:
    return xdg_config_dir() / "config.json"


def read_config_db_path() -> str | None:
    path = config_json_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("db_path") if isinstance(data, dict) else None
    return str(value) if value else None


def resolve_db(explicit_db: Path | None = None) -> tuple[Path, str]:
    if explicit_db is not None:
        return expand_path(explicit_db), "--db"

    for key in DB_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return expand_path(value), f"env:{key}"

    for env_file in harness_env_files():
        values = parse_env_file(env_file)
        for key in DB_ENV_KEYS:
            if key in values:
                return expand_path(values[key]), f"{env_file}:{key}"

    configured = read_config_db_path()
    if configured:
        return expand_path(configured), str(config_json_path())

    project_deck = Path.cwd() / "sr" / "cards.sqlite"
    if project_deck.exists():
        return project_deck, "existing project deck"

    return xdg_data_dir() / "cards.sqlite", "default user data dir"


def write_config(db: Path) -> Path:
    path = config_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"db_path": str(expand_path(db)), "updated_at": iso(now())}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_readonly(db: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(db), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = DELETE;
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cards (
          id TEXT PRIMARY KEY,
          deck TEXT NOT NULL,
          card_type TEXT NOT NULL,
          front TEXT NOT NULL,
          back TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          source TEXT NOT NULL,
          priority TEXT NOT NULL,
          semantic_key TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          supersedes_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS card_state (
          card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
          due_at TEXT NOT NULL,
          interval_days REAL NOT NULL,
          ease_factor REAL NOT NULL,
          repetitions INTEGER NOT NULL,
          lapses INTEGER NOT NULL,
          last_reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
          reviewed_at TEXT NOT NULL,
          answer TEXT,
          recall_grade INTEGER NOT NULL CHECK (recall_grade BETWEEN 0 AND 5),
          old_due_at TEXT,
          new_due_at TEXT NOT NULL,
          old_interval_days REAL,
          new_interval_days REAL NOT NULL,
          old_ease_factor REAL,
          new_ease_factor REAL NOT NULL,
          grader_notes TEXT
        );
        CREATE TABLE IF NOT EXISTS card_revisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          card_id TEXT NOT NULL,
          revised_at TEXT NOT NULL,
          reason TEXT NOT NULL,
          old_front TEXT,
          old_back TEXT,
          new_front TEXT,
          new_back TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_card_state_due ON card_state(due_at);
        CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews(card_id, reviewed_at);
        """
    )
    add_column_if_missing(conn, "cards", "semantic_key", "TEXT")
    add_column_if_missing(conn, "cards", "supersedes_json", "TEXT NOT NULL DEFAULT '[]'")
    version = conn.execute("SELECT value FROM meta WHERE key = 'semantic_key_version'").fetchone()
    if not version or version["value"] != SEMANTIC_KEY_VERSION:
        rows = conn.execute("SELECT id, front FROM cards").fetchall()
    else:
        rows = conn.execute("SELECT id, front FROM cards WHERE semantic_key IS NULL OR semantic_key = ''").fetchall()
    for row in rows:
        conn.execute("UPDATE cards SET semantic_key = ? WHERE id = ?", (semantic_key(row["front"]), row["id"]))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_active_semantic ON cards(active, semantic_key)")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '1')")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('semantic_key_version', ?)",
        (SEMANTIC_KEY_VERSION,),
    )


def initial_due(priority: str) -> datetime:
    base = now()
    if priority == "P0":
        return base
    if priority == "P1":
        return base + timedelta(days=1)
    if priority == "P2":
        return base + timedelta(days=3)
    return base + timedelta(days=7)


def load_cards(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "cards" in data:
        data = data["cards"]
    if not isinstance(data, list):
        raise SystemExit("Card JSON must be a list or an object with a cards list.")
    return data


def add_cards(conn: sqlite3.Connection, cards: list[dict], deck: str | None, deactivate_duplicates: bool, emit: bool = True) -> dict:
    stamp = iso(now())
    added = updated = superseded = 0
    for raw in cards:
        card_id = str(raw.get("id") or "").strip()
        front = str(raw.get("front") or "").strip()
        back = str(raw.get("back") or "").strip()
        if not card_id or not front or not back:
            raise SystemExit(f"Card missing id/front/back: {raw!r}")
        priority = normalize_priority(raw.get("priority") or "P2")
        card_type = str(raw.get("type") or raw.get("card_type") or "basic")
        source = str(raw.get("source") or "unknown")
        tags = normalize_tags(raw.get("tags"))
        skey = str(raw.get("semantic_key") or semantic_key(front))
        supersedes = [str(value) for value in raw.get("supersedes", [])]
        superseded_ids: set[str] = set()
        explicit_reactivate = raw.get("active") is True or raw.get("reactivate") is True
        deck_value = deck or raw.get("deck") or "Default"
        tags_json = json.dumps(tags, sort_keys=True)
        existing = conn.execute(
            """
            SELECT deck, card_type, front, back, tags_json, source, priority, semantic_key,
                   active, supersedes_json
            FROM cards WHERE id = ?
            """,
            (card_id,),
        ).fetchone()
        desired_active = 1
        if existing and existing["active"] == 0 and not explicit_reactivate:
            desired_active = 0

        if desired_active:
            if deactivate_duplicates:
                rows = conn.execute(
                    "SELECT id FROM cards WHERE active = 1 AND semantic_key = ? AND id != ?",
                    (skey, card_id),
                ).fetchall()
                for row in rows:
                    conn.execute("UPDATE cards SET active = 0, updated_at = ? WHERE id = ?", (stamp, row["id"]))
                    supersedes.append(row["id"])
                    superseded_ids.add(row["id"])

            for old_id in supersedes:
                result = conn.execute("UPDATE cards SET active = 0, updated_at = ? WHERE id = ? AND active = 1", (stamp, old_id))
                if result.rowcount:
                    superseded_ids.add(old_id)

        supersedes_json = json.dumps(sorted(set(supersedes)))
        content_changed = False
        priority_changed = False
        stored_changed = False
        if existing:
            if existing["front"] != front or existing["back"] != back:
                content_changed = True
                conn.execute(
                    """
                    INSERT INTO card_revisions(card_id, revised_at, reason, old_front, old_back, new_front, new_back)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (card_id, stamp, "add-json update", existing["front"], existing["back"], front, back),
                )
            priority_changed = existing["priority"] != priority
            stored_changed = any(
                [
                    existing["deck"] != deck_value,
                    existing["card_type"] != card_type,
                    existing["front"] != front,
                    existing["back"] != back,
                    existing["tags_json"] != tags_json,
                    existing["source"] != source,
                    existing["priority"] != priority,
                    existing["semantic_key"] != skey,
                    existing["active"] != desired_active,
                    existing["supersedes_json"] != supersedes_json,
                ]
            )
            if stored_changed:
                updated += 1
        else:
            added += 1

        conn.execute(
            """
            INSERT INTO cards(id, deck, card_type, front, back, tags_json, source, priority, semantic_key,
                              active, supersedes_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              deck=excluded.deck,
              card_type=excluded.card_type,
              front=excluded.front,
              back=excluded.back,
              tags_json=excluded.tags_json,
              source=excluded.source,
              priority=excluded.priority,
              semantic_key=excluded.semantic_key,
              active=excluded.active,
              supersedes_json=excluded.supersedes_json,
              updated_at=excluded.updated_at
            """,
            (
                card_id,
                deck_value,
                card_type,
                front,
                back,
                tags_json,
                source,
                priority,
                skey,
                desired_active,
                supersedes_json,
                stamp,
                stamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO card_state(card_id, due_at, interval_days, ease_factor, repetitions, lapses, last_reviewed_at)
            VALUES (?, ?, 0, 2.5, 0, 0, NULL)
            ON CONFLICT(card_id) DO NOTHING
            """,
            (card_id, iso(initial_due(priority))),
        )
        if content_changed or priority_changed:
            conn.execute(
                """
                UPDATE card_state
                SET due_at = ?, interval_days = 0, ease_factor = 2.5,
                    repetitions = 0, lapses = 0, last_reviewed_at = NULL
                WHERE card_id = ?
                """,
                (iso(initial_due(priority)), card_id),
            )
        superseded += len(superseded_ids)
    conn.commit()
    summary = {"added": added, "updated": updated, "superseded": superseded}
    if emit:
        print(json.dumps(summary, indent=2))
    return summary


def active_cards_columns(conn: sqlite3.Connection | None) -> set[str]:
    if conn is None or not table_exists(conn, "cards"):
        return set()
    return columns(conn, "cards")


def check_cards(conn: sqlite3.Connection | None, cards: list[dict], deck: str | None, limit: int) -> None:
    duplicate_ids: dict[str, int] = {}
    duplicate_semantic_keys: dict[str, list[str]] = {}
    existing_ids = []
    existing_semantic_matches = []
    seen_ids: set[str] = set()
    seen_keys: dict[str, str] = {}
    missing = []
    invalid = []
    card_cols = active_cards_columns(conn)
    has_cards = conn is not None and bool(card_cols)
    can_check_ids = has_cards and "id" in card_cols
    can_check_semantic = has_cards and {"id", "front"}.issubset(card_cols)

    for index, raw in enumerate(cards):
        if not isinstance(raw, dict):
            invalid.append({"index": index, "error": "card must be a JSON object"})
            continue
        card_id = str(raw.get("id") or "").strip()
        front = str(raw.get("front") or "").strip()
        back = str(raw.get("back") or "").strip()
        if not card_id or not front or not back:
            missing.append({"index": index, "id": card_id, "front": front[:120], "has_back": bool(back)})
            continue
        try:
            normalize_priority(raw.get("priority") or "P2")
        except SystemExit as exc:
            invalid.append({"index": index, "id": card_id, "error": str(exc)})
        supersedes = raw.get("supersedes", [])
        if not isinstance(supersedes, list):
            invalid.append({"index": index, "id": card_id, "error": "supersedes must be a list when present"})
        tags = raw.get("tags", [])
        if not isinstance(tags, (list, str)):
            invalid.append({"index": index, "id": card_id, "error": "tags must be a list or string when present"})

        if card_id in seen_ids:
            duplicate_ids[card_id] = duplicate_ids.get(card_id, 1) + 1
        seen_ids.add(card_id)

        skey = str(raw.get("semantic_key") or semantic_key(front))
        if skey in seen_keys:
            duplicate_semantic_keys.setdefault(skey, [seen_keys[skey]]).append(card_id)
        else:
            seen_keys[skey] = card_id

        if can_check_ids:
            active_select = ", active" if "active" in card_cols else ""
            existing = conn.execute(f"SELECT id{active_select} FROM cards WHERE id = ?", (card_id,)).fetchone()
            if existing:
                existing_ids.append(
                    {
                        "id": existing["id"],
                        "active": bool(existing["active"]) if "active" in existing.keys() else True,
                    }
                )

        if can_check_semantic:
            match_rows = semantic_matches(conn, card_cols, skey, card_id, limit)
            if match_rows:
                existing_semantic_matches.append(
                    {
                        "proposed_id": card_id,
                        "semantic_key": skey,
                        "matches": match_rows,
                    }
                )

    ok = not missing and not invalid and not duplicate_ids and not duplicate_semantic_keys
    print(
        json.dumps(
            {
                "ok": ok,
                "proposed_count": len(cards),
                "deck_override": deck,
                "db_checked": has_cards,
                "duplicate_scope": "active cards across all decks",
                "missing_required_fields": missing[:limit],
                "invalid_cards": invalid[:limit],
                "duplicate_ids": duplicate_ids,
                "duplicate_semantic_keys": duplicate_semantic_keys,
                "existing_id_matches": existing_ids[:limit],
                "existing_semantic_matches": existing_semantic_matches[:limit],
                "truncated": {
                    "missing_required_fields": len(missing) > limit,
                    "invalid_cards": len(invalid) > limit,
                    "existing_id_matches": len(existing_ids) > limit,
                    "existing_semantic_matches": len(existing_semantic_matches) > limit,
                },
            },
            indent=2,
        )
    )
    if not ok:
        raise SystemExit(1)


def semantic_matches(conn: sqlite3.Connection, card_cols: set[str], skey: str, proposed_id: str, limit: int) -> list[dict]:
    select_fields = ["id", "front"]
    for optional in ("deck", "source"):
        if optional in card_cols:
            select_fields.append(optional)
    where = "WHERE id != ?"
    params: list[object] = [proposed_id]
    if "active" in card_cols:
        where += " AND active = 1"
    if "semantic_key" in card_cols:
        where += " AND semantic_key = ?"
        params.append(skey)
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_fields)}
        FROM cards
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    matches = []
    for row in rows:
        if "semantic_key" not in card_cols and semantic_key(row["front"]) != skey:
            continue
        item = dict(row)
        item.setdefault("deck", "")
        item.setdefault("source", "")
        matches.append(item)
        if len(matches) >= limit:
            break
    return matches


def sm2(old_interval: float, old_ease: float, reps: int, lapses: int, grade: int) -> tuple[float, float, int, int]:
    new_ease = max(1.3, old_ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))
    if grade < 3:
        return 1.0, new_ease, 0, lapses + 1
    if reps == 0:
        interval = 1.0
    elif reps == 1:
        interval = 3.0
    else:
        interval = max(1.0, old_interval * new_ease)
    return interval, new_ease, reps + 1, lapses


def due(conn: sqlite3.Connection, limit: int, tag: str | None, include_back: bool = False) -> None:
    if limit < 1:
        raise SystemExit("--limit must be at least 1")
    params: list[object] = [iso(now())]
    tag_clause = ""
    if tag:
        tag_clause = "AND c.tags_json LIKE ?"
        params.append(f'%"{tag.lower()}"%')
    params.append(limit)
    answer_column = ", c.back" if include_back else ""
    rows = conn.execute(
        f"""
        SELECT c.id, c.front, c.priority, c.tags_json, c.source, s.due_at{answer_column}
        FROM cards c JOIN card_state s ON s.card_id = c.id
        WHERE c.active = 1 AND s.due_at <= ? {tag_clause}
        ORDER BY CASE c.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                 s.due_at, c.id
        LIMIT ?
        """,
        params,
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        if not include_back:
            item["front"] = prompt_front(item["front"])
        output.append(item)
    print(json.dumps(output, indent=2))


def reveal(conn: sqlite3.Connection, card_id: str) -> None:
    row = conn.execute(
        "SELECT id, back, source FROM cards WHERE id = ? AND active = 1",
        (card_id,),
    ).fetchone()
    if not row:
        inactive = conn.execute("SELECT active FROM cards WHERE id = ?", (card_id,)).fetchone()
        if inactive and inactive["active"] == 0:
            raise SystemExit(f"Card is inactive or superseded: {card_id}")
        raise SystemExit(f"Unknown card id: {card_id}")
    print(json.dumps(dict(row), indent=2))


def record_review(conn: sqlite3.Connection, card_id: str, grade: int, answer: str, notes: str) -> None:
    row = conn.execute(
        """
        SELECT s.due_at, s.interval_days, s.ease_factor, s.repetitions, s.lapses
        FROM card_state s JOIN cards c ON c.id = s.card_id
        WHERE c.id = ? AND c.active = 1
        """,
        (card_id,),
    ).fetchone()
    if not row:
        inactive = conn.execute("SELECT active FROM cards WHERE id = ?", (card_id,)).fetchone()
        if inactive and inactive["active"] == 0:
            raise SystemExit(f"Card is inactive or superseded: {card_id}")
        raise SystemExit(f"Unknown card id: {card_id}")
    reviewed_at = now()
    interval, ease, reps, lapses = sm2(row["interval_days"], row["ease_factor"], row["repetitions"], row["lapses"], grade)
    new_due = iso(reviewed_at + timedelta(days=interval))
    conn.execute(
        """
        UPDATE card_state
        SET due_at = ?, interval_days = ?, ease_factor = ?, repetitions = ?,
            lapses = ?, last_reviewed_at = ?
        WHERE card_id = ?
        """,
        (new_due, interval, ease, reps, lapses, iso(reviewed_at), card_id),
    )
    conn.execute(
        """
        INSERT INTO reviews(card_id, reviewed_at, answer, recall_grade, old_due_at,
                            new_due_at, old_interval_days, new_interval_days,
                            old_ease_factor, new_ease_factor, grader_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            iso(reviewed_at),
            answer,
            grade,
            row["due_at"],
            new_due,
            row["interval_days"],
            interval,
            row["ease_factor"],
            ease,
            notes,
        ),
    )
    conn.commit()
    print(json.dumps({"card_id": card_id, "grade": grade, "next_due": new_due, "interval_days": interval}, indent=2))


def export_json(conn: sqlite3.Connection, out: Path) -> None:
    rows = conn.execute(
        """
        SELECT c.*, s.due_at, s.interval_days, s.ease_factor, s.repetitions, s.lapses, s.last_reviewed_at
        FROM cards c JOIN card_state s ON s.card_id = c.id
        WHERE c.active = 1
        ORDER BY c.priority, c.id
        """
    ).fetchall()
    cards = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json"))
        item["supersedes"] = json.loads(item.pop("supersedes_json"))
        cards.append(item)
    out.write_text(json.dumps({"generated_at": iso(now()), "cards": cards}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")


def stats_data(conn: sqlite3.Connection) -> dict:
    priority = conn.execute("SELECT priority, COUNT(*) n FROM cards WHERE active = 1 GROUP BY priority ORDER BY priority").fetchall()
    due_count = conn.execute("SELECT COUNT(*) n FROM cards c JOIN card_state s ON s.card_id = c.id WHERE c.active = 1 AND s.due_at <= ?", (iso(now()),)).fetchone()["n"]
    reviews = conn.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"]
    return {"active_by_priority": {row["priority"]: row["n"] for row in priority}, "due_now": due_count, "reviews": reviews}


def stats(conn: sqlite3.Connection) -> None:
    print(json.dumps(stats_data(conn), indent=2))


def search_cards(conn: sqlite3.Connection, query: str, limit: int, include_back: bool) -> None:
    if limit < 1:
        raise SystemExit("--limit must be at least 1")
    terms = [term.lower() for term in re.findall(r"[a-zA-Z0-9_:-]+", query) if term.strip()]
    if not terms:
        raise SystemExit("--query must contain at least one searchable term")
    card_cols = active_cards_columns(conn)
    if not card_cols:
        print("[]")
        return
    required = {"id", "deck", "front", "back", "tags_json", "source", "priority"}
    missing = sorted(required - card_cols)
    if missing:
        raise SystemExit(f"Deck schema is missing columns required for search: {', '.join(missing)}")

    select_fields = ["id", "deck", "front", "back", "tags_json", "source", "priority"]
    if "semantic_key" in card_cols:
        select_fields.append("semantic_key")
    where = "WHERE active = 1" if "active" in card_cols else ""
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_fields)}
        FROM cards
        {where}
        ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                 id
        """
    ).fetchall()
    matches = []
    for row in rows:
        haystack = " ".join(
            [
                row["id"],
                row["deck"],
                row["front"],
                row["back"],
                row["tags_json"],
                row["source"],
                row["semantic_key"] if "semantic_key" in row.keys() else semantic_key(row["front"]),
            ]
        ).lower()
        if all(term in haystack for term in terms):
            item = {
                "id": row["id"],
                "deck": row["deck"],
                "front": row["front"],
                "tags": json.loads(row["tags_json"]),
                "source": row["source"],
                "priority": row["priority"],
                "semantic_key": row["semantic_key"] if "semantic_key" in row.keys() else semantic_key(row["front"]),
            }
            if include_back:
                item["back"] = row["back"]
            matches.append(item)
            if len(matches) >= limit:
                break
    print(json.dumps(matches, indent=2))


def print_where(db: Path, source: str) -> None:
    config_path = config_json_path()
    existing_project = Path.cwd() / "sr" / "cards.sqlite"
    print(
        json.dumps(
            {
                "db": str(db),
                "source": source,
                "config_json": str(config_path),
                "env_keys": list(DB_ENV_KEYS),
                "env_files_checked": [str(path) for path in harness_env_files()],
                "existing_project_deck": str(existing_project) if existing_project.exists() else None,
            },
            indent=2,
        )
    )


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, help="Override deck path. Defaults to the user-level learn config.")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", parents=[common])
    configure = sub.add_parser("configure")
    configure.add_argument("--db", type=Path, required=True)
    sub.add_parser("where", parents=[common])

    add = sub.add_parser("add-json", parents=[common])
    add.add_argument("--cards", type=Path, required=True)
    add.add_argument("--deck")
    add.add_argument("--no-deactivate-duplicates", action="store_true")
    add.add_argument("--dry-run", action="store_true", help="Apply to a temporary copy and leave the configured deck unchanged.")

    check = sub.add_parser("check-json", parents=[common])
    check.add_argument("--cards", type=Path, required=True)
    check.add_argument("--deck")
    check.add_argument("--limit", type=int, default=20)

    search = sub.add_parser("search", parents=[common])
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--include-back", action="store_true")

    due_cmd = sub.add_parser("due", parents=[common])
    due_cmd.add_argument("--limit", type=int, default=10)
    due_cmd.add_argument("--tag")
    due_cmd.add_argument("--include-back", action="store_true")

    reveal_cmd = sub.add_parser("reveal", parents=[common])
    reveal_cmd.add_argument("--card-id", required=True)

    rec = sub.add_parser("record-review", parents=[common])
    rec.add_argument("--card-id", required=True)
    rec.add_argument("--grade", type=int, required=True)
    rec.add_argument("--answer")
    rec.add_argument("--answer-file", type=Path)
    rec.add_argument("--notes", default="")

    exp = sub.add_parser("export-json", parents=[common])
    exp.add_argument("--out", type=Path, required=True)

    sub.add_parser("stats", parents=[common])

    args = parser.parse_args()

    if args.cmd == "configure":
        db = expand_path(args.db)
        path = write_config(db)
        print(json.dumps({"config": str(path), "db": str(db)}, indent=2))
        return

    db, db_source = resolve_db(args.db)
    if args.cmd == "where":
        print_where(db, db_source)
        return

    if args.cmd == "search":
        if not db.exists():
            print("[]")
            return
        conn = connect_readonly(db)
        try:
            search_cards(conn, args.query, args.limit, args.include_back)
        finally:
            conn.close()
        return

    if args.cmd == "check-json":
        cards = load_cards(args.cards)
        conn = connect_readonly(db) if db.exists() else None
        try:
            check_cards(conn, cards, args.deck, args.limit)
        finally:
            if conn is not None:
                conn.close()
        return

    if args.cmd == "add-json" and args.dry_run:
        cards = load_cards(args.cards)
        with tempfile.TemporaryDirectory(prefix="agent-skill-learn-") as temp_dir:
            temp_db = Path(temp_dir) / "cards.sqlite"
            if db.exists():
                shutil.copy2(db, temp_db)
            temp_conn = connect(temp_db)
            try:
                create_schema(temp_conn)
                temp_conn.commit()
                result = add_cards(temp_conn, cards, args.deck, not args.no_deactivate_duplicates, emit=False)
                print(
                    json.dumps(
                        {
                            "dry_run": True,
                            "db": str(db),
                            "result": result,
                            "stats_after": stats_data(temp_conn),
                        },
                        indent=2,
                    )
                )
            finally:
                temp_conn.close()
        return

    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    create_schema(conn)
    conn.commit()

    if args.cmd == "init":
        print(f"initialized {db}")
    elif args.cmd == "add-json":
        cards = load_cards(args.cards)
        add_cards(conn, cards, args.deck, not args.no_deactivate_duplicates)
    elif args.cmd == "due":
        due(conn, args.limit, args.tag, args.include_back)
    elif args.cmd == "reveal":
        reveal(conn, args.card_id)
    elif args.cmd == "record-review":
        if not 0 <= args.grade <= 5:
            raise SystemExit("--grade must be 0..5")
        answer = args.answer or ""
        if args.answer_file:
            answer = args.answer_file.read_text(encoding="utf-8")
        record_review(conn, args.card_id, args.grade, answer, args.notes)
    elif args.cmd == "export-json":
        export_json(conn, args.out)
    elif args.cmd == "stats":
        stats(conn)
    else:
        raise AssertionError(args.cmd)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
