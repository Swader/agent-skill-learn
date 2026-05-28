#!/usr/bin/env python3
"""SQLite spaced repetition helper for agent-skill-learn."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


def add_cards(conn: sqlite3.Connection, cards: list[dict], deck: str | None, deactivate_duplicates: bool) -> None:
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
    print(json.dumps({"added": added, "updated": updated, "superseded": superseded}, indent=2))


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
    print(json.dumps([dict(row) for row in rows], indent=2))


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


def stats(conn: sqlite3.Connection) -> None:
    priority = conn.execute("SELECT priority, COUNT(*) n FROM cards WHERE active = 1 GROUP BY priority ORDER BY priority").fetchall()
    due_count = conn.execute("SELECT COUNT(*) n FROM cards c JOIN card_state s ON s.card_id = c.id WHERE c.active = 1 AND s.due_at <= ?", (iso(now()),)).fetchone()["n"]
    reviews = conn.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"]
    print(json.dumps({"active_by_priority": {row["priority"]: row["n"] for row in priority}, "due_now": due_count, "reviews": reviews}, indent=2))


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

    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    create_schema(conn)
    conn.commit()

    if args.cmd == "init":
        print(f"initialized {db}")
    elif args.cmd == "add-json":
        add_cards(conn, load_cards(args.cards), args.deck, not args.no_deactivate_duplicates)
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
