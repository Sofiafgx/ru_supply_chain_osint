"""Transactional import of explicitly observed, complete browser DOM posts."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from social_collection import SocialArchive, canonical, encoded, platform_name, stamp


def official_url(value, platform, handle, post_id=None):
    if not isinstance(value, str):
        raise ValueError("An official HTTPS URL is required")
    parts = urlsplit(value)
    hosts = {"X": {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"},
             "Threads": {"threads.net", "www.threads.net", "threads.com", "www.threads.com"}}
    if parts.scheme != "https" or parts.hostname not in hosts[platform] or parts.username or parts.password or parts.port:
        raise ValueError("URL must use an official platform HTTPS host")
    if post_id is not None:
        pattern = r"/([A-Za-z0-9_]+)/status/([0-9]+)/?" if platform == "X" else r"/@([A-Za-z0-9_.]+)/post/([A-Za-z0-9_-]+)/?"
        match = re.fullmatch(pattern, parts.path)
        if not match or match[1].casefold() != handle.lstrip("@").casefold() or match[2] != post_id:
            raise ValueError("Post permalink must match account_handle and post_id")
    return value


def identity(row):
    platform = platform_name(row.get("platform"))
    handle = row.get("account_handle")
    pattern = r"@[A-Za-z0-9_]+" if platform == "X" else r"@[A-Za-z0-9_.]+"
    if not platform or not isinstance(handle, str) or not re.fullmatch(pattern, handle):
        raise ValueError("A supported platform and explicit @account_handle are required")
    return platform, handle


def validate_post(row):
    if not isinstance(row, dict):
        raise ValueError("Each post must be an object")
    platform, handle = identity(row)
    if row.get("source_mode") != "browser_dom" or row.get("text_complete") is not True:
        raise ValueError("Only complete original browser_dom text is accepted; text_complete must be true")
    if row.get("text_truncated") or row.get("is_truncated") or row.get("is_repost") or row.get("original_text") is False:
        raise ValueError("Truncated or repost text cannot be imported as original post text")
    if not isinstance(row.get("text"), str) or not isinstance(row.get("timestamp"), str) or not row["timestamp"]:
        raise ValueError("Original text and an explicit publication timestamp are required")
    identifier = str(row.get("post_id") or "")
    official_url(row.get("url"), platform, handle, identifier)
    post, _ = canonical(dict(row, platform=platform, post_id=identifier))
    return post


def validate_observation(row):
    if not isinstance(row, dict):
        raise ValueError("Each observation must be an object")
    platform, handle = identity(row)
    official_url(row.get("url"), platform, handle)
    if not isinstance(row.get("status"), str) or not row["status"].strip() or not isinstance(row.get("detail"), str):
        raise ValueError("Observation requires status and detail strings")
    if row["status"].casefold() in {"complete", "completed", "synced", "fully_synced", "100%", "full_archive", "api_exhausted"}:
        raise ValueError("Browser observations cannot assert complete historical coverage")
    result = dict(row, platform=platform)
    if "date_range" in row:
        bounds = row["date_range"]
        if not isinstance(bounds, dict) or not bounds.get("start") or not bounds.get("end"):
            raise ValueError("Observation date_range requires explicit start and end")
        start, end = stamp(bounds["start"]), stamp(bounds["end"])
        if start >= end:
            raise ValueError("Observation date_range must be a positive half-open interval")
        result["date_range"] = {"start": start, "end": end}
    return result


def setup(archive):
    archive.db.executescript("""
        CREATE TABLE IF NOT EXISTS browser_batches (
            sha256 TEXT PRIMARY KEY, imported_at TEXT NOT NULL, receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS browser_observations (
            batch_sha256 TEXT NOT NULL, position INTEGER NOT NULL, platform TEXT NOT NULL,
            account_handle TEXT NOT NULL, json TEXT NOT NULL,
            PRIMARY KEY(batch_sha256,position));
        CREATE TABLE IF NOT EXISTS browser_checkpoints (
            batch_sha256 TEXT PRIMARY KEY, json TEXT NOT NULL);
    """)


def import_batch(archive, path):
    """Commit every post, observation and checkpoint together, or commit none."""
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    batch = json.loads(raw)
    if not isinstance(batch, dict) or not isinstance(batch.get("posts"), list) or not isinstance(batch.get("observations"), list):
        raise ValueError("Batch requires posts and observations arrays")
    posts = [validate_post(row) for row in batch["posts"]]
    observations = [validate_observation(row) for row in batch["observations"]]
    if "checkpoint" in batch and not isinstance(batch["checkpoint"], dict):
        raise ValueError("Checkpoint must be an object")
    setup(archive)
    archive.db.execute("BEGIN IMMEDIATE")
    try:
        previous = archive.db.execute("SELECT receipt FROM browser_batches WHERE sha256=?", (digest,)).fetchone()
        if previous:
            archive.db.rollback()
            return dict(json.loads(previous[0]), already_imported=True)
        new = skipped = 0
        for post in posts:
            result = archive.index_record(post, "browser_batch:" + digest, save=True)
            if result == "NEW":
                new += 1
            else:
                skipped += 1
        receipt = {"batch_sha256": digest, "new_posts": new, "skipped_existing": skipped,
                   "observations": len(observations), "already_imported": False}
        archive.db.execute("INSERT INTO browser_batches VALUES(?,?,?)", (digest, stamp(), encoded(receipt)))
        for position, row in enumerate(observations):
            archive.db.execute("INSERT INTO browser_observations VALUES(?,?,?,?,?)",
                               (digest, position, row["platform"], row["account_handle"].casefold(), encoded(row)))
        if "checkpoint" in batch:
            archive.db.execute("INSERT INTO browser_checkpoints VALUES(?,?)", (digest, encoded(batch["checkpoint"])))
        archive.db.commit()
        return receipt
    except Exception:
        archive.db.rollback()
        raise


def atomic_write(path, text):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def export_scope(archive):
    path = archive.directory / "browser_scope.json"
    scope = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    start = stamp(scope["start"]) if scope.get("start") else None
    return start


def export_post_files(archive):
    """Keep one decoded post in memory; publish only fully written files."""
    temporary_paths = []
    destinations = [archive.directory / "browser_posts.jsonl", archive.directory / "browser_posts.csv"]
    try:
        with ExitStack() as stack:
            handles = []
            for destination in destinations:
                descriptor, temporary = tempfile.mkstemp(prefix="." + destination.name, dir=str(destination.parent))
                temporary_paths.append(temporary)
                handles.append(stack.enter_context(os.fdopen(descriptor, "w", encoding="utf-8", newline="")))
            jsonl, output = handles
            output.write("\ufeff")
            fields = ["platform", "post_id", "account_handle", "author_id", "timestamp", "text", "url", "content_hash", "source_mode", "text_complete"]
            writer = csv.DictWriter(output, fields, extrasaction="ignore")
            writer.writeheader()
            start = export_scope(archive)
            rows = archive.db.execute("SELECT json FROM posts WHERE json_extract(json,'$.source_mode')='browser_dom' AND (? IS NULL OR timestamp>=?) ORDER BY platform,timestamp,post_id", (start, start))
            for row in rows:
                post = json.loads(row[0])
                jsonl.write(encoded(post) + "\n")
                writer.writerow(post)
        for temporary, destination in zip(temporary_paths, destinations):
            os.replace(temporary, destination)
    finally:
        for temporary in temporary_paths:
            if os.path.exists(temporary):
                os.unlink(temporary)


def export_browser(archive):
    setup(archive)
    start = export_scope(archive)
    observations = [json.loads(row[0]) for row in archive.db.execute("SELECT json FROM browser_observations ORDER BY rowid")]
    accounts = {}
    saved_posts = 0
    for row in archive.db.execute("""SELECT platform,MIN(account_handle) account_handle,COUNT(*) saved_posts
            FROM posts WHERE json_extract(json,'$.source_mode')='browser_dom'
            AND (? IS NULL OR timestamp>=?)
            GROUP BY platform,lower(account_handle)""", (start, start)):
        key = (row["platform"], row["account_handle"].casefold())
        accounts[key] = {"platform": key[0], "account_handle": row["account_handle"],
                         "status": "partial_capture", "detail": "Browser posts recorded; historical coverage unverified",
                         "saved_posts": row["saved_posts"]}
        saved_posts += row["saved_posts"]
    for observation in observations:
        key = (observation["platform"], observation["account_handle"].casefold())
        count = accounts.get(key, {}).get("saved_posts", 0)
        accounts[key] = dict(observation, saved_posts=count)
    checkpoint = archive.db.execute("SELECT batch_sha256,json FROM browser_checkpoints ORDER BY rowid DESC LIMIT 1").fetchone()
    report = {"generated_at": stamp(), "source_mode": "browser_dom", "completeness": "not_verified", "requested_start": start,
              "note": "Visible DOM observations do not establish full historical coverage. Missing, inaccessible and deleted posts remain unknown.",
              "saved_browser_posts": saved_posts, "batch_count": archive.db.execute("SELECT COUNT(*) FROM browser_batches").fetchone()[0],
              "accounts": list(accounts.values()), "observations": observations,
              "latest_checkpoint": json.loads(checkpoint["json"]) if checkpoint else None,
              "checkpoint_batch_sha256": checkpoint["batch_sha256"] if checkpoint else None}
    export_post_files(archive)
    atomic_write(archive.directory / "browser_coverage.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# Browser collection", "", "Historical completeness: **not verified**.", "",
             "Requested start (inclusive, UTC): {}.".format(start or "account creation"), "",
             "Saved unique browser posts: **{}**. Imported batches: **{}**.".format(saved_posts, report["batch_count"]), "",
             "| Platform | Account | Latest observed status | Saved posts | Detail |", "|---|---|---|---:|---|"]
    for account in report["accounts"]:
        clean = lambda value: str(value).replace("|", "/").replace("\n", " ").replace("\r", " ")
        lines.append("| {} | {} | {} | {} | {} |".format(*(clean(account.get(key, "")) for key in ("platform", "account_handle", "status", "saved_posts", "detail"))))
    lines += ["", report["note"], "", "The checkpoint is available in browser_coverage.json. API jobs and coverage.json are not modified."]
    atomic_write(archive.directory / "BROWSER_REPORT.md", "\n".join(lines) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--directory", default="data/social_history")
    args = parser.parse_args(argv)
    archive = SocialArchive(args.directory)
    try:
        receipt = import_batch(archive, args.batch)
        export_browser(archive)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    finally:
        archive.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
