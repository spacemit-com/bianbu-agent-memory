#!/usr/bin/env python3
"""Plain-SQLite replica of the long_term FTS tables (MEM-NFR-01-01).

Reproduces the exact table shapes and the bench corpus (k3_perf_acceptance
make_text with the same seeded rng) so the L1 / lexical-backfill query shapes
can be timed and EXPLAINed without SQLCipher in the way. The 1.27s
lexical-backfill measured for the pure-numeric query "4074" is the target.

    env PYTHONPATH=sdk/python/src python3 scripts/k3_fts_replica.py [--rebuild]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from k3_perf_acceptance import MODULES, make_text  # noqa: E402

DB = Path("/tmp/fts-replica.db")
RECORDS = 100_000
KINDS = ("profile", "preference", "fact", "project", "episodic", "decision", "reference")
NAMESPACE = ("acct", "agent", "space")

MEM_DDL = """
CREATE TABLE IF NOT EXISTS long_term_memories (
    memory_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL, agent_id TEXT NOT NULL, memory_space_id TEXT NOT NULL,
    kind TEXT NOT NULL, content TEXT NOT NULL, summary TEXT, tags_json TEXT NOT NULL,
    source TEXT NOT NULL, confidence REAL, sensitivity TEXT NOT NULL,
    current_revision INTEGER NOT NULL, sync_policy TEXT NOT NULL,
    expires_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    hlc TEXT NOT NULL
)
"""
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS long_term_fts USING fts5(
    memory_id UNINDEXED, content, summary, tags, tokenize = 'trigram'
)
"""
FTS2_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS long_term_fts2 USING fts5(
    memory_id UNINDEXED, terms, tokenize = 'unicode61'
)
"""
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_long_term_namespace
ON long_term_memories(account_id, agent_id, memory_space_id, kind, updated_at DESC)
"""


def lexical_tokens(text: str, summary: str, tags_json: str) -> str:
    """Python mirror of long_term.rs lexical_tokens."""
    combined = f"{text} {summary} {tags_json}".lower()
    tokens: list[str] = []

    def flush(run: str) -> None:
        if len(run) >= 2:
            if run.isascii():
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    run = ""
    for character in combined:
        if character.isalnum():
            run += character
        else:
            flush(run)
            run = ""
    flush(run)
    return " ".join(tokens)


def build(conn: sqlite3.Connection) -> None:
    conn.execute(MEM_DDL)
    conn.execute(FTS_DDL)
    conn.execute(FTS2_DDL)
    conn.execute(INDEX_DDL)
    rng = random.Random(20260907)
    started = time.monotonic()
    conn.execute("BEGIN")
    for index in range(RECORDS):
        text = make_text(index, rng)
        memory_id = f"m{index:06d}"
        tags = '["perf-seed"]'
        conn.execute(
            "INSERT INTO long_term_memories"
            "(memory_id,account_id,agent_id,memory_space_id,kind,content,summary,tags_json,"
            "source,confidence,sensitivity,current_revision,sync_policy,expires_at,"
            "created_at,updated_at,hlc) "
            "VALUES(?,?,?,?,?,?,?,?,'source',0.9,'normal',1,'local_only',NULL,?,?,?)",
            (memory_id, *NAMESPACE, KINDS[index % len(KINDS)], text, None, tags,
             index, index, f"hlc{index}"),
        )
        conn.execute(
            "INSERT INTO long_term_fts(memory_id,content,summary,tags) VALUES(?,?,?,?)",
            (memory_id, text, None, tags),
        )
        conn.execute(
            "INSERT INTO long_term_fts2(rowid, memory_id, terms) VALUES(?,?,?)",
            (index + 1, memory_id, lexical_tokens(text, "", tags)),
        )
        if (index + 1) % 10_000 == 0:
            print(f"built {index + 1}/{RECORDS} {time.monotonic() - started:.0f}s",
                  file=sys.stderr)
    conn.commit()


def timed(conn: sqlite3.Connection, label: str, sql: str, params: tuple) -> None:
    plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    for row in plan:
        print(f"  plan: {row[-1]}")
    before = time.perf_counter_ns()
    rows = conn.execute(sql, params).fetchall()
    elapsed = (time.perf_counter_ns() - before) / 1_000_000
    print(f"{label}: {elapsed:.1f}ms ({len(rows)} rows)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    if args.rebuild and DB.exists():
        DB.unlink()
    fresh = not DB.exists()
    conn = sqlite3.connect(DB)
    print(f"sqlite {sqlite3.sqlite_version} fresh={fresh}")
    if fresh:
        build(conn)

    ns = NAMESPACE + (int(time.time() * 1000),)
    trigram_l1 = (
        "SELECT l.memory_id,l.kind,l.summary,l.current_revision,l.sync_policy,l.source,"
        "l.sensitivity,l.updated_at,l.hlc,l.content,l.tags_json "
        "FROM long_term_fts "
        "JOIN long_term_memories l ON l.memory_id=long_term_fts.memory_id "
        "WHERE long_term_fts MATCH ? AND l.account_id=? AND l.agent_id=? "
        "AND l.memory_space_id=? AND (l.expires_at IS NULL OR expires_at>?) "
        "ORDER BY long_term_fts.rowid DESC LIMIT ?"
    )
    trigram_backfill = (
        "SELECT l.memory_id FROM long_term_fts "
        "JOIN long_term_memories l ON l.memory_id=long_term_fts.memory_id "
        "WHERE long_term_fts MATCH ? AND l.account_id=? AND l.agent_id=? "
        "AND l.memory_space_id=? AND (l.expires_at IS NULL OR expires_at>?) "
        "ORDER BY long_term_fts.rowid DESC LIMIT ?"
    )
    in_fetch = (
        "SELECT memory_id,kind,summary,current_revision,sync_policy,source,sensitivity,"
        "updated_at,hlc,content,summary,tags_json FROM long_term_memories "
        "WHERE account_id=? AND agent_id=? AND memory_space_id=? "
        "AND (expires_at IS NULL OR expires_at>?) AND memory_id IN (?,?,?,?,?,?,?) "
        "ORDER BY updated_at DESC"
    )
    fts2_l1 = (
        "SELECT l.memory_id,l.kind,l.summary,l.current_revision,l.sync_policy,l.source,"
        "l.sensitivity,l.updated_at,l.hlc,l.content,l.tags_json "
        "FROM long_term_fts2 f "
        "JOIN long_term_memories l ON l.rowid=f.rowid "
        "WHERE f.terms MATCH ? AND l.account_id=? AND l.agent_id=? "
        "AND l.memory_space_id=? AND (l.expires_at IS NULL OR expires_at>?) "
        "ORDER BY f.rowid DESC LIMIT ?"
    )

    ids = tuple(
        row[0]
        for row in conn.execute(
            "SELECT memory_id FROM long_term_memories WHERE content LIKE '%4074%' "
            "ORDER BY memory_id LIMIT 7"
        )
    )
    print(f"ids containing 4074: {len(ids)}")
    battery = [
        ("A  trigram L1 shape '4074' LIMIT 100", trigram_l1, ('"4074"', *ns, 100)),
        ("A2 trigram L1 shape '4074' LIMIT 200", trigram_l1, ('"4074"', *ns, 200)),
        ("B  trigram backfill shape '4074' LIMIT 200", trigram_backfill, ('"4074"', *ns, 200)),
        ("B2 trigram backfill shape '4074' LIMIT 100", trigram_backfill, ('"4074"', *ns, 100)),
        ("C  IN() fetch 7 ids", in_fetch, (*ns, *ids)),
        ("D  trigram L1 'docs' LIMIT 100", trigram_l1, ('"docs"', *ns, 100)),
        ("E  trigram AND 'docs'+'4074' LIMIT 200", trigram_backfill, ('"docs" AND "4074"', *ns, 200)),
        ("F  fts2 L1 '浏览 览器' LIMIT 100", fts2_l1, ('"浏览 览器"', *ns, 100)),
    ]
    for round_ in (1, 2):
        print(f"===== round {round_}")
        for label, sql, params in battery:
            print(f"== {label}")
            timed(conn, label, sql, params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
