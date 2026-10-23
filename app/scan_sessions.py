#!/usr/bin/env python3
"""Incrementally index Codex session metadata without storing message content."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
ROOTS = (HOME / ".codex" / "sessions", HOME / ".codex" / "archived_sessions")
DATA_DIR = HOME / ".codex" / "my-usage"
DB_PATH = DATA_DIR / "usage.sqlite3"
COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
RELEVANT_HEADER = re.compile(r'^\s*\{[^{}]{0,250}"type"\s*:\s*"(session_meta|turn_context|token_usage_record|event_msg)"')
SESSION_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f-]{27,})$", re.I)


def zero_counters():
    return {key: 0 for key in COUNTERS}


def add_counters(target, usage):
    if not isinstance(usage, dict):
        return
    for key in COUNTERS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value


def ensure_model(models, model, effort=None):
    entry = models.setdefault(model, {"turns": 0, "efforts": {}, "tokens": zero_counters()})
    if effort:
        entry["turns"] += 1
        entry["efforts"][effort] = entry["efforts"].get(effort, 0) + 1
    return entry


def initial_session(file_path):
    stem = Path(file_path).stem
    match = SESSION_ID.search(stem)
    return {
        "id": match.group(1) if match else stem,
        "timestamp": None,
        "cwd": "",
        "source": "",
        "models": {},
        "turns": 0,
        "tokens": zero_counters(),
        "tokenSource": "unavailable",
    }


def scan_file(file_path):
    session = initial_session(file_path)
    turn_models = {}
    turn_usage_snapshots = {}
    response_usages = {}
    fallback_by_turn = {}
    with open(file_path, "r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            header = line[:320]
            match = RELEVANT_HEADER.match(header)
            if not match:
                continue
            row_type = match.group(1)
            if row_type == "event_msg" and '"token_count"' not in header:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            payload = row.get("payload") or {}
            if row_type == "session_meta":
                session["id"] = payload.get("session_id") or payload.get("id") or session["id"]
                session["timestamp"] = payload.get("timestamp") or row.get("timestamp") or session["timestamp"]
                session["cwd"] = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
                session["source"] = payload.get("source") if isinstance(payload.get("source"), str) else ""
            elif row_type == "turn_context":
                model = payload.get("model") if isinstance(payload.get("model"), str) else "unknown"
                effort = payload.get("effort") if isinstance(payload.get("effort"), str) else "unknown"
                turn_id = payload.get("turn_id") or f"ordinal:{row.get('ordinal')}"
                turn_models[turn_id] = {"model": model, "effort": effort}
                ensure_model(session["models"], model, effort)
                session["turns"] += 1
            elif row_type == "token_usage_record":
                response_id = payload.get("response_id") or f"{payload.get('turn_id')}:{row.get('ordinal')}"
                turn_id = payload.get("turn_id")
                turn_usage = payload.get("turn_token_usage")
                response_usage = payload.get("usage")
                if isinstance(turn_usage, dict) and turn_usage:
                    # turn_token_usage is cumulative and repeated as tool/model
                    # responses advance; retain the last snapshot for each turn.
                    turn_usage_snapshots[turn_id or response_id] = (turn_id, turn_usage)
                elif isinstance(response_usage, dict) and response_usage:
                    # Older records without a turn snapshot report per-response usage.
                    response_usages[response_id] = (turn_id, response_usage)
            elif payload.get("type") == "token_count":
                info = payload.get("info") or {}
                turn_id = payload.get("turn_id")
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if usage and turn_id:
                    ordinal = row.get("ordinal") or 0
                    old = fallback_by_turn.get(turn_id)
                    if old is None or ordinal >= old[0]:
                        fallback_by_turn[turn_id] = (ordinal, usage)

    if turn_usage_snapshots or response_usages:
        snapshot_turns = {turn_id for turn_id, _ in turn_usage_snapshots.values() if turn_id}
        for turn_id, usage in turn_usage_snapshots.values():
            add_counters(session["tokens"], usage)
            model = turn_models.get(turn_id, {}).get("model", "unknown")
            add_counters(ensure_model(session["models"], model)["tokens"], usage)
        for turn_id, usage in response_usages.values():
            if turn_id and turn_id in snapshot_turns:
                continue
            add_counters(session["tokens"], usage)
            model = turn_models.get(turn_id, {}).get("model", "unknown")
            add_counters(ensure_model(session["models"], model)["tokens"], usage)
        session["tokenSource"] = "turn usage snapshots" if turn_usage_snapshots else "response records"
    elif fallback_by_turn:
        for turn_id, (_, usage) in fallback_by_turn.items():
            add_counters(session["tokens"], usage)
            model = turn_models.get(turn_id, {}).get("model", "unknown")
            add_counters(ensure_model(session["models"], model)["tokens"], usage)
        session["tokenSource"] = "per-turn snapshots"
    return session


def connect_db():
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    existing = db.execute("PRAGMA table_info(sessions)").fetchall()
    needs_rebuild = bool(existing and next((column["pk"] for column in existing if column["name"] == "session_id"), 0))
    if needs_rebuild:
        # Version 0.1 indexed one row per chat and could overwrite multi-file chats.
        # Rebuild from source so the new index can retain every file part.
        db.execute("DROP TABLE sessions")
    db.execute("""CREATE TABLE IF NOT EXISTS sessions (
        file_path TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        mtime_ns INTEGER NOT NULL,
        size_bytes INTEGER NOT NULL,
        timestamp TEXT,
        cwd TEXT NOT NULL,
        source TEXT NOT NULL,
        turns INTEGER NOT NULL,
        models_json TEXT NOT NULL,
        tokens_json TEXT NOT NULL,
        token_source TEXT NOT NULL
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if needs_rebuild:
        db.execute("DELETE FROM state WHERE key = 'last_scan'")
    db.commit()
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        pass
    return db


def get_state(db, key):
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def sync_index(if_stale=False):
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = connect_db()
    started = time.time()
    database_version = db.execute("PRAGMA user_version").fetchone()[0]
    if database_version < 3:
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM state WHERE key = 'last_scan'")
        db.execute("PRAGMA user_version = 3")
        db.commit()
    previous = get_state(db, "last_scan")
    if if_stale and previous:
        try:
            if started - datetime.fromisoformat(previous).timestamp() < 24 * 60 * 60:
                db.close()
                return {"indexedFiles": 0, "unchangedFiles": 0, "deletedSessions": 0, "skipped": True, "lastScan": previous, "databasePath": str(DB_PATH)}
        except ValueError:
            pass

    paths = []
    for root in ROOTS:
        if root.exists():
            for base, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                paths.extend(str(Path(base) / name) for name in names if name.endswith(".jsonl") and not (Path(base) / name).is_symlink())
    paths.sort()
    present = set(paths)
    indexed = unchanged = deleted = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        known = {row["file_path"]: (row["mtime_ns"], row["size_bytes"]) for row in db.execute("SELECT file_path, mtime_ns, size_bytes FROM sessions")}
        for file_path in paths:
            try:
                stat = os.stat(file_path, follow_symlinks=False)
            except OSError:
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            if known.get(file_path) == signature:
                unchanged += 1
                continue
            try:
                session = scan_file(file_path)
            except (OSError, ValueError):
                continue
            db.execute("""INSERT INTO sessions(file_path,session_id,mtime_ns,size_bytes,timestamp,cwd,source,turns,models_json,tokens_json,token_source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(file_path) DO UPDATE SET session_id=excluded.session_id,mtime_ns=excluded.mtime_ns,size_bytes=excluded.size_bytes,timestamp=excluded.timestamp,cwd=excluded.cwd,source=excluded.source,turns=excluded.turns,models_json=excluded.models_json,tokens_json=excluded.tokens_json,token_source=excluded.token_source""",
                (file_path, str(session["id"]), stat.st_mtime_ns, stat.st_size, session["timestamp"], session["cwd"], session["source"], session["turns"], json.dumps(session["models"], separators=(",", ":")), json.dumps(session["tokens"], separators=(",", ":")), session["tokenSource"]))
            indexed += 1
        for row in db.execute("SELECT file_path FROM sessions").fetchall():
            if row["file_path"] not in present:
                db.execute("DELETE FROM sessions WHERE file_path = ?", (row["file_path"],))
                deleted += 1
        scanned_at = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO state(key,value) VALUES('last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (scanned_at,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    write_dashboard_snapshot()
    return {"indexedFiles": indexed, "unchangedFiles": unchanged, "deletedSessions": deleted, "skipped": False, "lastScan": scanned_at, "databasePath": str(DB_PATH)}


def write_dashboard_snapshot():
    # Keep the directly opened file useful too. Prefer the editable plugin source
    # over the versioned cache where MCP may execute this script from.
    candidates = (HOME / ".codex/plugins/my-usage-plugin/app/dashboard.html", Path(__file__).with_name("dashboard.html"))
    dashboard = next((path for path in candidates if path.is_file()), None)
    if dashboard is None:
        return
    html = dashboard.read_text(encoding="utf-8")
    start_marker, end_marker = "<!-- MY_USAGE_SNAPSHOT_START -->", "<!-- MY_USAGE_SNAPSHOT_END -->"
    start, end = html.find(start_marker), html.find(end_marker)
    if start < 0 or end < start:
        return
    payload = json.dumps(read_index(), separators=(",", ":")).replace("<", "\\u003c")
    block = f'{start_marker}<script type="application/json" id="usage-snapshot">{payload}</script>{end_marker}'
    updated = html[:start] + block + html[end + len(end_marker):]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dashboard.parent, delete=False) as output:
        output.write(updated)
        temporary = Path(output.name)
    os.replace(temporary, dashboard)


def read_index():
    db = connect_db()
    try:
        rows = db.execute("SELECT session_id,timestamp,cwd,source,turns,models_json,tokens_json,token_source FROM sessions ORDER BY COALESCE(timestamp,'')").fetchall()
        grouped = {}
        for row in rows:
            session = grouped.setdefault(row["session_id"], {
                "id": row["session_id"], "timestamp": row["timestamp"], "cwd": row["cwd"], "source": row["source"],
                "turns": 0, "models": {}, "tokens": zero_counters(), "tokenSource": "unavailable", "_token_sources": set(),
            })
            if row["timestamp"] and (not session["timestamp"] or row["timestamp"] < session["timestamp"]):
                session["timestamp"] = row["timestamp"]
            if not session["cwd"] and row["cwd"]:
                session["cwd"] = row["cwd"]
            if not session["source"] and row["source"]:
                session["source"] = row["source"]
            session["turns"] += row["turns"]
            file_tokens = json.loads(row["tokens_json"])
            add_counters(session["tokens"], file_tokens)
            if row["token_source"] != "unavailable":
                session["_token_sources"].add(row["token_source"])
            file_models = json.loads(row["models_json"])
            for model, model_data in file_models.items():
                aggregate_model = session["models"].setdefault(model, {"turns": 0, "efforts": {}, "tokens": zero_counters()})
                aggregate_model["turns"] += model_data.get("turns", 0)
                add_counters(aggregate_model["tokens"], model_data.get("tokens"))
                for effort, count in model_data.get("efforts", {}).items():
                    aggregate_model["efforts"][effort] = aggregate_model["efforts"].get(effort, 0) + count
        sessions = list(grouped.values())
        for session in sessions:
            if session["_token_sources"]:
                session["tokenSource"] = next(iter(session["_token_sources"])) if len(session["_token_sources"]) == 1 else "mixed sources"
            del session["_token_sources"]
        sessions.sort(key=lambda session: session["timestamp"] or "", reverse=True)
        totals = {"sessions": 0, "sessionsWithTokenData": 0, "tokens": zero_counters(), "models": {}, "efforts": {}, "tokenSources": {}, "scannedAt": get_state(db, "last_scan")}
        for session in sessions:
            models = session["models"]
            tokens = session["tokens"]
            totals["sessions"] += 1
            totals["tokenSources"][session["tokenSource"]] = totals["tokenSources"].get(session["tokenSource"], 0) + 1
            if session["tokenSource"] != "unavailable":
                totals["sessionsWithTokenData"] += 1
            add_counters(totals["tokens"], tokens)
            for model, model_data in models.items():
                aggregate = totals["models"].setdefault(model, {"sessions": 0, "turns": 0, "efforts": {}, "tokens": zero_counters()})
                aggregate["sessions"] += 1
                aggregate["turns"] += model_data.get("turns", 0)
                add_counters(aggregate["tokens"], model_data.get("tokens"))
                for effort, count in model_data.get("efforts", {}).items():
                    aggregate["efforts"][effort] = aggregate["efforts"].get(effort, 0) + count
                    totals["efforts"][effort] = totals["efforts"].get(effort, 0) + count
        return {"totals": totals, "sessions": sessions}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("sync", "read"))
    parser.add_argument("--if-stale", action="store_true", help="Skip the scan if the index was scanned less than 24 hours ago")
    args = parser.parse_args()
    result = sync_index(if_stale=args.if_stale) if args.command == "sync" else read_index()
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except Exception as error:
        print(f"my-usage scan failed: {error}", file=sys.stderr)
        sys.exit(1)
