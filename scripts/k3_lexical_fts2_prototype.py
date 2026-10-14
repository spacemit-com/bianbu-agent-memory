#!/usr/bin/env python3
"""MEM-NFR-01-01 design prototype: bigram FTS2 table for the lexical cascade.

Validates on K3 (plain sqlite3, no daemon) the three load-bearing
assumptions of the planned fix BEFORE touching Rust:

  1. fts2 (unicode61, segmented bigram+word tokens) with
     `MATCH ... ORDER BY f.rowid DESC LIMIT N` early-stops instead of
     ranking every match (no bm25 in the query);
  2. the JOIN back to the parent table for ~100-200 candidates is cheap;
  3. the AND query (LIKE-backfill replacement) returns instantly when
     nothing matches, and exactly after Rust-side substring verify.

Also measures segmentation cost and DELETE ... WHERE memory_id=? cost at
100k rows for both the trigram table and fts2 (the revise path).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from k3_perf_acceptance import make_text  # noqa: E402


def segment(text: str, summary: str | None, tags_json: str) -> str:
    """Rust `lexical_tokens` prototype: per-run bigrams (CJK) / words (ASCII).

    Mirrors the planned segmentation: alphanumeric runs; CJK runs >=2 chars
    emit all bigrams; ASCII runs >=2 emit the lowercased word; 1-char runs
    emit nothing.  Space-joined.
    """
    tokens: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 2:
            if all(character.isascii() for character in run):
                tokens.append("".join(run))
            else:
                tokens.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
        run.clear()

    for character in (text + " " + (summary or "") + " " + tags_json):
        if character.isalnum():
            run.append(character)
        else:
            flush()
    flush()
    return " ".join(tokens)


def query_runs(query: str) -> list[tuple[str, str]]:
    """(kind, token-string) per alphanumeric run: cjk / word2 / tri (ascii>=3)."""
    runs: list[tuple[str, str]] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 2:
            if all(character.isascii() for character in run):
                runs.append(("tri" if len(run) >= 3 else "word2", "".join(run)))
            else:
                bigrams = [run[i] + run[i + 1] for i in range(len(run) - 1)]
                runs.append(("cjk", " ".join(bigrams)))
        run.clear()

    for character in query.lower():
        if character.isalnum():
            run.append(character)
        else:
            flush()
    flush()
    return runs


def fts2_match(runs: list[tuple[str, str]]) -> str | None:
    parts = [
        '"{}"'.format(tokens.replace('"', '""'))
        for kind, tokens in runs
        if kind in ("cjk", "word2")
    ]
    return " OR ".join(parts) if parts else None


def trigram_match(runs: list[tuple[str, str]]) -> str | None:
    parts = ['"{}"'.format(tokens.replace('"', '""')) for kind, tokens in runs if kind == "tri"]
    return " OR ".join(parts) if parts else None


QUERIES = (
    "终端 配色", "浏览器 默认", "构建 命令", "深色 主题", "密钥 管理", "部署 步骤",
    "docs-4231 存储层", "密钥管理 规范", "密钥管理", "终端",
)


def main() -> int:
    records = 100_000
    report: dict[str, object] = {"status": "K3_LEXICAL_FTS2_PROTOTYPE", "records": records}
    with tempfile.TemporaryDirectory(prefix="fts2-proto-") as raw:
        path = Path(raw) / "proto.db"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE long_term_memories (
                memory_id TEXT PRIMARY KEY, account_id TEXT, agent_id TEXT,
                memory_space_id TEXT, expires_at INTEGER, sensitivity TEXT,
                updated_at INTEGER, content TEXT, summary TEXT, tags_json TEXT)"""
        )
        connection.execute(
            "CREATE VIRTUAL TABLE long_term_fts2 USING fts5(memory_id UNINDEXED, terms, tokenize='unicode61')"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE long_term_fts USING fts5(memory_id UNINDEXED, content, tokenize='trigram')"
        )

        started = time.monotonic()
        rows = []
        import random

        rng = random.Random(20260907)
        for index in range(records):
            text = make_text(index, rng)
            rows.append((f"m{index:06d}", "a", "g", "s", None, "normal", index, text, None, '["perf-seed"]'))
        report["corpus_s"] = round(time.monotonic() - started, 1)

        started = time.monotonic()
        connection.executemany(
            "INSERT INTO long_term_memories VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )
        report["parent_insert_s"] = round(time.monotonic() - started, 2)

        started = time.monotonic()
        connection.executemany(
            "INSERT INTO long_term_fts2(memory_id, terms) VALUES(?,?)",
            [(r[0], segment(r[7], r[8], r[9])) for r in rows],
        )
        report["fts2_insert_s"] = round(time.monotonic() - started, 2)

        started = time.monotonic()
        connection.executemany(
            "INSERT INTO long_term_fts(memory_id, content) VALUES(?,?)",
            [(r[0], r[7]) for r in rows],
        )
        report["fts_insert_s"] = round(time.monotonic() - started, 2)
        connection.commit()

        # query plans
        for name, sql, params in [
            ("fts2_or", "SELECT l.memory_id FROM long_term_fts2 f JOIN long_term_memories l "
                        "ON l.memory_id=f.memory_id WHERE f.terms MATCH ?1 "
                        "AND l.account_id='a' AND (l.expires_at IS NULL OR l.expires_at>9) "
                        "ORDER BY f.rowid DESC LIMIT ?", ("终端", 200)),
            ("fts_and", "SELECT l.memory_id FROM long_term_fts2 f JOIN long_term_memories l "
                        "ON l.memory_id=f.memory_id WHERE f.terms MATCH ?1 "
                        "AND l.account_id='a' ORDER BY f.rowid DESC LIMIT ?", ('"终端" AND "配色"', 50)),
        ]:
            plan = connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            report[f"plan_{name}"] = [row[3] for row in plan]

        results = []
        for query in QUERIES:
            runs = query_runs(query)
            entry: dict[str, object] = {"query": query, "runs": runs}
            total_ms = 0.0
            for label, match in (("fts2", fts2_match(runs)), ("tri", trigram_match(runs))):
                if match is None:
                    continue
                sql = (
                    "SELECT l.memory_id,l.content,l.summary,l.tags_json,l.updated_at "
                    "FROM long_term_fts2 f JOIN long_term_memories l ON l.memory_id=f.memory_id "
                    "WHERE f.terms MATCH ?1 AND l.account_id='a' AND l.agent_id='g' "
                    "AND l.memory_space_id='s' AND (l.expires_at IS NULL OR l.expires_at>9) "
                    "AND l.sensitivity!='secret' ORDER BY f.rowid DESC LIMIT ?2"
                    if label == "fts2"
                    else "SELECT l.memory_id,l.content,l.summary,l.tags_json,l.updated_at "
                         "FROM long_term_fts f JOIN long_term_memories l ON l.memory_id=f.memory_id "
                         "WHERE f.content MATCH ?1 AND l.account_id='a' AND l.agent_id='g' "
                         "AND l.memory_space_id='s' AND (l.expires_at IS NULL OR l.expires_at>9) "
                         "AND l.sensitivity!='secret' ORDER BY f.rowid DESC LIMIT ?2"
                )
                started = time.perf_counter_ns()
                rows_fetched = connection.execute(sql, (match, 200)).fetchall()
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                total_ms += elapsed
                entry[label] = {"match": match, "ms": round(elapsed, 2), "rows": len(rows_fetched)}
            # AND backfill replacement (only for multi-run queries)
            if len(runs) >= 1:
                match = " AND ".join(
                    '"{}"'.format(tokens.replace('"', '""'))
                    for kind, tokens in runs
                    if kind in ("cjk", "word2")
                )
                if match:
                    sql = (
                        "SELECT l.memory_id,l.content,l.summary,l.tags_json,l.updated_at "
                        "FROM long_term_fts2 f JOIN long_term_memories l ON l.memory_id=f.memory_id "
                        "WHERE f.terms MATCH ?1 AND l.account_id='a' AND l.agent_id='g' "
                        "AND l.memory_space_id='s' AND (l.expires_at IS NULL OR l.expires_at>9) "
                        "AND l.sensitivity!='secret' ORDER BY f.rowid DESC LIMIT ?2"
                    )
                    started = time.perf_counter_ns()
                    rows_fetched = connection.execute(sql, (match, 50)).fetchall()
                    elapsed = (time.perf_counter_ns() - started) / 1_000_000
                    total_ms += elapsed
                    entry["and"] = {"match": match, "ms": round(elapsed, 2), "rows": len(rows_fetched)}
            entry["total_ms"] = round(total_ms, 2)
            results.append(entry)
            print(f"{query!r}: {entry['total_ms']} ms", file=sys.stderr)
        report["queries"] = results

        # DELETE cost at 100k (revise path pattern)
        for table in ("long_term_fts", "long_term_fts2"):
            started = time.perf_counter_ns()
            connection.execute(f"DELETE FROM {table} WHERE memory_id='m050000'")
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            report[f"delete_{table}_ms"] = round(elapsed, 2)
        connection.commit()

        db_bytes = path.stat().st_size
        report["db_bytes"] = db_bytes

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
