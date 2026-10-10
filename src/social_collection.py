"""Exact disk-backed deduplication and atomic, resumable social collection jobs.

Coverage describes the API traversal, never proof that deleted/withheld posts exist.
Time intervals are half-open. Historical jobs do not stop at duplicate streaks.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def stamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def platform_name(value):
    return {"x": "X", "twitter": "X", "threads": "Threads"}.get(str(value).lower())


def windows(start, end, months=3):
    if not 1 <= months <= 3:
        raise ValueError("Use 1–3 month windows")
    current = datetime.fromisoformat(stamp(start).replace("Z", "+00:00"))
    stop = datetime.fromisoformat(stamp(end).replace("Z", "+00:00"))
    while current < stop:
        month = current.month - 1 + months
        year, month = current.year + month // 12, month % 12 + 1
        next_date = min(current.replace(year=year, month=month,
                        day=min(current.day, calendar.monthrange(year, month)[1])), stop)
        yield {"start": stamp(current), "end": stamp(next_date)}
        current = next_date


def canonical(record):
    platform = platform_name(record.get("platform"))
    post_id = str(record.get("post_id") or "")
    author = str(record.get("author_id") or record.get("account_handle") or "").lstrip("@").casefold()
    if not platform or not post_id or not author or not record.get("timestamp") or "text" not in record:
        raise ValueError("Post requires platform, post_id, author, timestamp and text")
    text = unicodedata.normalize("NFC", str(record["text"]).replace("\r\n", "\n"))
    timestamp = stamp(record["timestamp"])
    key = encoded([platform, author, text, timestamp])
    result = dict(record, platform=platform, post_id=post_id, timestamp=timestamp, text=text)
    result["content_hash"] = hashlib.md5(key.encode("utf-8")).hexdigest()
    return result, key


class SocialArchive:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "social.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS existing_ids (
                platform TEXT NOT NULL, post_id TEXT NOT NULL, origin TEXT NOT NULL,
                PRIMARY KEY(platform,post_id));
            CREATE TABLE IF NOT EXISTS existing_content (
                platform TEXT NOT NULL, content_hash TEXT NOT NULL,
                canonical TEXT NOT NULL, origin TEXT NOT NULL,
                PRIMARY KEY(platform,content_hash,canonical));
            CREATE TABLE IF NOT EXISTS posts (
                platform TEXT NOT NULL, post_id TEXT NOT NULL, account_handle TEXT NOT NULL,
                timestamp TEXT NOT NULL, content_hash TEXT NOT NULL, json TEXT NOT NULL,
                PRIMARY KEY(platform,post_id));
            CREATE TABLE IF NOT EXISTS jobs (
                task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                cursor TEXT, pages INTEGER NOT NULL DEFAULT 0,
                new_posts INTEGER NOT NULL DEFAULT 0, skipped_existing INTEGER NOT NULL DEFAULT 0,
                duplicate_streak INTEGER NOT NULL DEFAULT 0,
                last_post_id TEXT, last_timestamp TEXT, cursor_restarts INTEGER NOT NULL DEFAULT 0,
                detail TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS visited_cursors (
                task_id TEXT NOT NULL, cursor TEXT NOT NULL, PRIMARY KEY(task_id,cursor));
            CREATE TABLE IF NOT EXISTS accounts (
                platform TEXT NOT NULL, handle TEXT NOT NULL, json TEXT NOT NULL,
                PRIMARY KEY(platform,handle));
        """)

    def contains(self, platform, post_id):
        return self.db.execute("SELECT 1 FROM existing_ids WHERE platform=? AND post_id=?",
                               (platform, str(post_id))).fetchone() is not None

    def index_record(self, record, origin, save=False):
        platform, identifier = platform_name(record.get("platform")), str(record.get("post_id") or "")
        if not platform or not identifier:
            raise ValueError("Missing platform/post_id")
        if self.contains(platform, identifier):
            return "SKIPPED_EXISTING"
        post, content = canonical(record)
        keys = [(post["content_hash"], content)]
        if record.get("author_id") and record.get("account_handle"):
            alias_post, alias_content = canonical(dict(record, author_id=None))
            keys.append((alias_post["content_hash"], alias_content))
        duplicate = any(self.db.execute("SELECT 1 FROM existing_content WHERE platform=? AND content_hash=? AND canonical=?",
                         (platform, digest, key)).fetchone() for digest, key in keys)
        self.db.execute("INSERT INTO existing_ids VALUES(?,?,?)", (platform, identifier, origin))
        for digest, key in keys:
            self.db.execute("INSERT OR IGNORE INTO existing_content VALUES(?,?,?,?)", (platform, digest, key, origin))
        if duplicate:
            return "SKIPPED_EXISTING"
        if save:
            self.db.execute("INSERT INTO posts VALUES(?,?,?,?,?,?)", (platform, identifier,
                str(post.get("account_handle") or post.get("author_id")), post["timestamp"], post["content_hash"], encoded(post)))
        return "NEW"

    def index_digest(self):
        digest = hashlib.sha256()
        for row in self.db.execute("SELECT platform,post_id FROM existing_ids ORDER BY platform,post_id"):
            digest.update((encoded(list(row)) + "\n").encode())
        for row in self.db.execute("SELECT platform,content_hash,canonical FROM existing_content ORDER BY platform,content_hash,canonical"):
            digest.update((encoded(list(row)) + "\n").encode())
        return digest.hexdigest()

    def inventory(self, project):
        """Read known project archives and post exports; never guess numeric ID namespaces."""
        project = Path(project).resolve()
        report = {"created_at": stamp(), "sources": [], "unrecognized": [], "errors": []}
        paths = sorted(set(project.rglob("*.sqlite")) | set(project.rglob("*.sqlite3")) | set(project.rglob("*.db")))
        for path in paths:
            if path.resolve() == self.path.resolve() or ".venv" in path.parts:
                continue
            source = {"path": str(path), "social_ids_indexed": 0}
            connection = None
            try:
                connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                connection.row_factory = sqlite3.Row
                tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                source["tables"] = sorted(tables)
                if "web_posts" in tables:
                    source["telegram_posts"] = connection.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]
                if "records" in tables:
                    source["record_counts"] = [dict(r) for r in connection.execute("SELECT source,kind,COUNT(*) count FROM records GROUP BY source,kind")]
                    for row in connection.execute("SELECT source,kind,id,json FROM records WHERE lower(source) IN ('x','twitter','threads')"):
                        if row["kind"].lower() not in ("post", "posts", "tweet", "tweets", "thread"):
                            source["excluded_non_post_records"] = source.get("excluded_non_post_records", 0) + 1
                            continue
                        data = json.loads(row["json"])
                        data = dict(data.get("data") or data)
                        data.setdefault("platform", row["source"])
                        data.setdefault("post_id", row["id"])
                        self._import_existing(data, str(path), source)
                if "posts" in tables:
                    columns = {r[1] for r in connection.execute("PRAGMA table_info(posts)")}
                    if {"platform", "post_id", "json"} <= columns:
                        for row in connection.execute("SELECT platform,post_id,json FROM posts"):
                            data = json.loads(row["json"])
                            data.update(platform=row["platform"], post_id=row["post_id"])
                            self._import_existing(data, str(path), source)
                    else:
                        report["unrecognized"].append({"path": str(path), "table": "posts"})
                if "existing_ids" in tables:
                    for row in connection.execute("SELECT platform,post_id FROM existing_ids"):
                        platform = platform_name(row["platform"])
                        if platform:
                            inserted = self.db.execute("INSERT OR IGNORE INTO existing_ids VALUES(?,?,?)", (platform, row["post_id"], str(path)))
                            source["social_ids_indexed"] += inserted.rowcount
                if "existing_content" in tables:
                    for row in connection.execute("SELECT platform,content_hash,canonical FROM existing_content"):
                        self.db.execute("INSERT OR IGNORE INTO existing_content VALUES(?,?,?,?)", (*row, str(path)))
                known = {"web_sources", "web_posts", "records", "jobs", "responses", "evidence", "source_status",
                         "posts", "existing_ids", "existing_content", "visited_cursors", "accounts",
                         "browser_batches", "browser_observations", "browser_checkpoints"}
                for table in tables & {"filtered_posts", "analyzed_posts"}:
                    columns = {r[1] for r in connection.execute("PRAGMA table_info(" + table + ")")}
                    if {"post_id", "record_json", "fingerprint"} <= columns:
                        unexpected = connection.execute("SELECT 1 FROM " + table + " WHERE post_id NOT LIKE 'public_web:%:%' LIMIT 1").fetchone()
                        if unexpected is None:
                            known.add(table)
                            source["telegram_derived_" + table] = connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in sorted(tables - known):
                    report["unrecognized"].append({"path": str(path), "table": table})
                self.db.commit()
            except (sqlite3.Error, ValueError, KeyError) as exc:
                self.db.rollback()
                report["errors"].append({"path": str(path), "error": str(exc)})
            finally:
                if connection is not None:
                    connection.close()
            report["sources"].append(source)
        # Export files have known names. Arbitrary files must be supplied via --import-posts.
        for path in sorted(set(project.rglob("posts.csv")) | set(project.rglob("posts.jsonl"))):
            if self.directory.resolve() in path.resolve().parents:
                continue
            try:
                report["sources"].append(self.import_posts(path))
            except (ValueError, OSError, csv.Error) as exc:
                report["errors"].append({"path": str(path), "error": str(exc)})
        report["existing_id_count"] = self.db.execute("SELECT COUNT(*) FROM existing_ids").fetchone()[0]
        report["existing_post_ids_hash"] = self.index_digest()
        report["ready"] = not report["errors"] and not report["unrecognized"]
        (self.directory / "inventory.json").write_text(encoded(report), encoding="utf-8")
        return report

    def _import_existing(self, row, origin, summary):
        platform = platform_name(row.get("platform") or row.get("source"))
        if not platform:
            raise ValueError("Social record with unknown platform")
        record = dict(row, platform=platform, post_id=row.get("post_id") or row.get("id"),
                      timestamp=row.get("timestamp") or row.get("created_at") or row.get("date"),
                      text=row.get("text", row.get("text_content", "")),
                      account_handle=row.get("account_handle") or row.get("author") or row.get("username"))
        if not record["post_id"]:
            raise ValueError("Social record missing ID")
        if self.contains(platform, record["post_id"]):
            return
        if record["timestamp"] and (record.get("author_id") or record["account_handle"]):
            self.index_record(record, origin)
        else:
            self.db.execute("INSERT OR IGNORE INTO existing_ids VALUES(?,?,?)", (platform, str(record["post_id"]), origin))
            summary["id_only_records"] = summary.get("id_only_records", 0) + 1
        summary["social_ids_indexed"] += 1

    def import_posts(self, path):
        path = Path(path)
        summary = {"path": str(path.resolve()), "social_ids_indexed": 0}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            if path.suffix == ".csv":
                reader = csv.DictReader(handle)
                if "source_mode" in (reader.fieldnames or []) and "channel_username" in reader.fieldnames:
                    summary["classification"] = "telegram_export_not_social_index"
                    return summary
            elif path.suffix == ".jsonl":
                reader = (json.loads(line) for line in handle if line.strip())
            else:
                raise ValueError("Expected CSV or JSONL")
            with self.db:
                for row in reader:
                    if not platform_name(row.get("platform") or row.get("source")) and (
                        str(row.get("platform") or row.get("source")).lower() == "telegram" or
                        {"source_mode", "channel_username", "message_id"} <= set(row)):
                        summary["telegram_records_excluded"] = summary.get("telegram_records_excluded", 0) + 1
                        continue
                    self._import_existing(row, str(path.resolve()), summary)
        return summary

    def save_account(self, account):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO accounts VALUES(?,?,?)",
                            (account["platform"], account["account_handle"].casefold(), encoded(account)))

    def plan_account(self, account, end, digest, months=3):
        # Unknown creation dates remain explicit, conservative planning lower bounds.
        start = account.get("created_at") or ("2006-03-21T00:00:00Z" if account["platform"] == "X" else "2023-07-05T00:00:00Z")
        basis = "account_created_at" if account.get("created_at") else "unverified_platform_lower_bound"
        status = account.get("status", "awaiting_account_metadata")
        with self.db:
            if status == "pending":
                # Once metadata is resolved, provisional jobs no longer represent the plan.
                for row in self.db.execute("SELECT task_id,payload FROM jobs WHERE status='awaiting_account_metadata'").fetchall():
                    old = json.loads(row["payload"])
                    if old["platform"] == account["platform"] and old["account_handle"].casefold() == account["account_handle"].casefold():
                        self.db.execute("UPDATE jobs SET status='superseded' WHERE task_id=?", (row["task_id"],))
            for date_range in windows(start, end, months):
                identity = [account["platform"], account["account_handle"].casefold(), account.get("account_id"), date_range]
                job_hash = hashlib.sha256(encoded(identity).encode()).hexdigest()[:20]
                task_id = "job_{}_{}_{}".format(account["platform"].lower(), re.sub(r"\W", "_", account["account_handle"].lstrip("@")), job_hash)
                task = {"task_id": task_id, "platform": account["platform"], "account_handle": account["account_handle"],
                        "target_account": account["account_handle"], "account_id": account.get("account_id"),
                        "date_range": date_range, "end_exclusive": True, "start_basis": basis,
                        "existing_post_ids_hash": digest, "existing_index": str(self.path.resolve()),
                        "skip_existing_ids": True, "max_consecutive_duplicates_stop": 20,
                        "early_stop_allowed": False, "mode": "historical"}
                self.db.execute("INSERT OR IGNORE INTO jobs(task_id,payload,status,updated_at) VALUES(?,?,?,?)",
                                (task_id, encoded(task), status, stamp()))
                if status == "pending":
                    self.db.execute("UPDATE jobs SET status='pending',payload=?,detail=NULL WHERE task_id=? AND status IN ('awaiting_account_metadata','blocked','superseded')",
                                    (encoded(task), task_id))

    def next_job(self):
        return self.db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()

    def commit_page(self, job, page):
        task = json.loads(job["payload"])
        if not isinstance(page.get("posts"), list) or not isinstance(page.get("exhausted"), bool):
            raise ValueError("Malformed provider page")
        cursor = page.get("next_cursor")
        if bool(cursor) == page["exhausted"]:
            raise ValueError("Inconsistent exhaustion/cursor")
        # A single BEGIN IMMEDIATE serializes dedup checks and page advancement across workers.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.db.execute("SELECT * FROM jobs WHERE task_id=?", (job["task_id"],)).fetchone()
            if current["cursor"] != job["cursor"] or current["pages"] != job["pages"] or current["status"] != "pending":
                raise ValueError("Stale worker checkpoint")
            if cursor and (cursor == job["cursor"] or self.db.execute("SELECT 1 FROM visited_cursors WHERE task_id=? AND cursor=?",
                                                              (job["task_id"], cursor)).fetchone()):
                raise ValueError("Repeated pagination cursor")
            new, skipped, streak = 0, 0, current["duplicate_streak"]
            last_id, last_time = current["last_post_id"], current["last_timestamp"]
            for post in page["posts"]:
                if platform_name(post.get("platform")) != task["platform"]:
                    raise ValueError("Provider returned wrong platform")
                timestamp = stamp(post.get("timestamp")) if post.get("timestamp") else None
                if timestamp is None:
                    raise ValueError("Missing post timestamp")
                if not stamp(task["date_range"]["start"]) <= timestamp < stamp(task["date_range"]["end"]):
                    continue
                if task.get("account_id") and str(post.get("author_id")) != str(task["account_id"]):
                    raise ValueError("Provider returned wrong author")
                result = self.index_record(post, job["task_id"], save=True)
                if result == "SKIPPED_EXISTING":
                    skipped += 1
                    streak += 1
                else:
                    new += 1
                    streak = 0
                last_id, last_time = str(post["post_id"]), timestamp
            status = "pending"
            if page["exhausted"]:
                status = "api_exhausted" if page.get("coverage_kind") == "full_archive" else "partial_source_exhausted"
            # Historical collection intentionally ignores the duplicate stopping heuristic.
            if task.get("early_stop_allowed") and task.get("mode") != "historical" and streak >= task["max_consecutive_duplicates_stop"] and not page["exhausted"]:
                status = "paused_duplicates_incomplete"
            if job["cursor"]:
                self.db.execute("INSERT OR IGNORE INTO visited_cursors VALUES(?,?)", (job["task_id"], job["cursor"]))
            self.db.execute("""UPDATE jobs SET cursor=?,status=?,pages=pages+1,new_posts=new_posts+?,
                skipped_existing=skipped_existing+?,duplicate_streak=?,last_post_id=?,last_timestamp=?,
                detail=?,updated_at=? WHERE task_id=?""", (cursor, status, new, skipped, streak,
                    last_id, last_time, encoded({"coverage_kind": page.get("coverage_kind")}), stamp(), job["task_id"]))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def failure(self, task_id, status, detail, expected=None):
        with self.db:
            if expected is None:
                self.db.execute("UPDATE jobs SET status=?,detail=?,updated_at=? WHERE task_id=?", (status, detail, stamp(), task_id))
            else:
                self.db.execute("UPDATE jobs SET status=?,detail=?,updated_at=? WHERE task_id=? AND cursor IS ? AND pages=? AND status=?",
                                (status, detail, stamp(), task_id, expected["cursor"], expected["pages"], expected["status"]))

    def restart_cursor(self, job):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.db.execute("SELECT * FROM jobs WHERE task_id=?", (job["task_id"],)).fetchone()
            if current["cursor"] != job["cursor"] or current["pages"] != job["pages"] or current["status"] != "pending":
                raise ValueError("Stale worker checkpoint")
            if job["cursor_restarts"] >= 1:
                self.db.execute("UPDATE jobs SET status='failed',detail=? WHERE task_id=?", ("Cursor expired repeatedly; manual retry required", job["task_id"]))
                self.db.commit()
                return
            self.db.execute("UPDATE jobs SET cursor=NULL,cursor_restarts=cursor_restarts+1,detail=? WHERE task_id=?",
                            ("Expired cursor: replay window with exact duplicate skip", job["task_id"]))
            self.db.execute("DELETE FROM visited_cursors WHERE task_id=?", (job["task_id"],))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def export(self, inventory=None, unresolved=()):
        jobs = []
        states = {}
        with (self.directory / "worker_tasks.jsonl").open("w", encoding="utf-8") as tasks:
            for row in self.db.execute("SELECT * FROM jobs ORDER BY rowid"):
                if row["status"] == "superseded":
                    continue
                task = json.loads(row["payload"])
                item = dict(task, status=row["status"], checkpoint={k: row[k] for k in
                    ("cursor", "pages", "last_post_id", "last_timestamp", "cursor_restarts")},
                    new_posts=row["new_posts"], skipped_existing=row["skipped_existing"], detail=row["detail"])
                tasks.write(encoded(item) + "\n")
                jobs.append({"task_id": row["task_id"], "status": row["status"]})
                states[row["status"]] = states.get(row["status"], 0) + 1
        with (self.directory / "new_posts.jsonl").open("w", encoding="utf-8") as handle:
            for row in self.db.execute("SELECT json FROM posts ORDER BY platform,post_id"):
                handle.write(row[0] + "\n")
        fields = ["platform", "post_id", "account_handle", "author_id", "timestamp", "text", "url", "content_hash"]
        with (self.directory / "new_posts.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fields, extrasaction="ignore")
            writer.writeheader()
            for row in self.db.execute("SELECT json FROM posts ORDER BY platform,post_id"):
                writer.writerow(json.loads(row[0]))
        accounts = [json.loads(row[0]) for row in self.db.execute("SELECT json FROM accounts ORDER BY platform,handle")]
        report = {"created_at": stamp(), "inventory": inventory, "accounts": accounts, "unresolved_accounts": list(unresolved),
                  "jobs_by_status": states, "job_count": len(jobs),
                  "new_posts": self.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                  "existing_id_count": self.db.execute("SELECT COUNT(*) FROM existing_ids").fetchone()[0],
                  "watermarks": [dict(row) for row in self.db.execute("SELECT platform,account_handle,MIN(timestamp) oldest_parsed_date,MAX(timestamp) latest_parsed_date FROM posts GROUP BY platform,account_handle")],
                  "all_windows_api_exhausted": bool(jobs) and set(states) == {"api_exhausted"} and not unresolved,
                  "completeness": "not_verified", "note": "API exhaustion is not proof of 100% of all public posts. Watermarks do not prove absence of gaps."}
        (self.directory / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def close(self):
        self.db.close()
