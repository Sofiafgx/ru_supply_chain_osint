"""Plan and resume public X/Threads collection without silently claiming completeness."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from social_collection import SocialArchive, encoded, stamp


TOKEN_KEYS = {"X": "X_BEARER_TOKEN", "Threads": "THREADS_ACCESS_TOKEN"}


def prepare(archive, configuration, end, digest, adapters, months, block_reason=None):
    for configured in configuration["accounts"]:
        account = dict(configured)
        platform = account["platform"]
        cached = archive.db.execute("SELECT json FROM accounts WHERE platform=? AND handle=?",
                                   (platform, account["account_handle"].casefold())).fetchone()
        if cached:
            previous = json.loads(cached[0])
            if previous.get("resolved"):
                account.update(previous)
        if not adapters.get(platform):
            reason = block_reason or ("Collection not started; use --run" if os.environ.get(TOKEN_KEYS[platform]) else "Missing " + TOKEN_KEYS[platform])
            account.update(status="blocked", detail=reason)
        elif not account.get("resolved"):
            try:
                account.update(adapters[platform].resolve_account(account["account_handle"]))
                account.update(resolved=True, status="pending", detail=None)
            except Exception as exc:
                # Adapter exceptions are sanitized; arbitrary exceptions must not print credentials.
                from parsers.social_api import SourceBlocked
                if isinstance(exc, SourceBlocked):
                    reason = str(exc)
                else:
                    reason = "Account lookup failed (" + type(exc).__name__ + ")"
                account.update(status="blocked", detail=reason)
        else:
            account.update(status="pending", detail=None)
        archive.save_account(account)
        plan_account = dict(account)
        if not account.get("resolved"):
            plan_account["status"] = "awaiting_account_metadata"
        archive.plan_account(plan_account, end, digest, months)


def run(archive, adapters, max_pages, delay=1.1):
    from parsers.social_api import CursorExpired, SourceBlocked
    attempted = 0
    blocked_reasons = {}
    while attempted < max_pages:
        job = archive.next_job()
        if job is None:
            break
        task = json.loads(job["payload"])
        adapter = adapters.get(task["platform"])
        if adapter is None:
            archive.failure(job["task_id"], "blocked", blocked_reasons.get(task["platform"], "Missing " + TOKEN_KEYS[task["platform"]]), expected=job)
            continue
        attempted += 1
        try:
            page = adapter.fetch_page(task, job["cursor"])
            archive.commit_page(job, page)
        except CursorExpired:
            archive.restart_cursor(job)
        except SourceBlocked as exc:
            archive.failure(job["task_id"], "blocked", str(exc), expected=job)
            # Avoid repeating a platform-level authorization/rate failure across all windows.
            adapters[task["platform"]] = None
            blocked_reasons[task["platform"]] = str(exc)
            for row in archive.db.execute("SELECT json FROM accounts WHERE platform=?", (task["platform"],)).fetchall():
                account = json.loads(row[0])
                account.update(status="blocked", detail=str(exc))
                archive.save_account(account)
        except (ValueError, httpx.HTTPError) as exc:
            archive.failure(job["task_id"], "failed", "Page failed (" + type(exc).__name__ + "); checkpoint retained", expected=job)
        if attempted < max_pages and archive.next_job() is not None:
            time.sleep(delay)
    return attempted


def write_report(archive, report):
    lines = ["# Збір X / Threads", "", "Повноту всієї історії не підтверджено.", "",
             "Нових збережених дописів: **{}**.".format(report["new_posts"]),
             "Унікальних ID в індексі проєкту: **{}**.".format(report["existing_id_count"]),
             "Часових завдань: **{}**.".format(report["job_count"]), "",
             "| Стан | Завдання |", "|---|---:|"]
    lines += ["| {} | {} |".format(key, count) for key, count in report["jobs_by_status"].items()]
    lines += ["", "| Платформа | Профіль | Стан | Причина |", "|---|---|---|---|"]
    for account in report["accounts"]:
        lines.append("| {} | {} | {} | {} |".format(account["platform"], account["account_handle"], account["status"], (account.get("detail") or "").replace("|", "/")))
    for account in report["unresolved_accounts"]:
        lines.append("| {} | {} | needs_confirmation | {} |".format(account["platform"], account["label"], account.get("reason", "")))
    lines += ["", "Вікна мають включний початок і виключний кінець. Непідтверджена дата створення позначена `unverified_platform_lower_bound`; це попередній план, а не встановлена дата створення акаунта.",
              "", "`api_exhausted` означає лише завершення пагінації повного архіву API. `partial_source_exhausted` не означає повноту історії. Відсутні ключі, приховані/видалені дописи, обмеження джерела та невизначені профілі не зараховуються до завершеного збору.",
              "", "Історичний збір не зупиняється після 20 дублікатів. Індекс SQLite перевіряє точний ID у межах платформи й хеш разом із канонічним вмістом. Чекпоїнт оновлюється в одній транзакції з дописами."]
    (archive.directory / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", default='config/social_accounts.json')
    parser.add_argument("--directory", default="data/social_history")
    parser.add_argument("--project", default=".")
    parser.add_argument("--end", help="Exclusive ISO timestamp; default is saved snapshot end, or now minus 60s")
    parser.add_argument("--months", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--run", action="store_true", help="Resolve profiles and collect with configured tokens")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--import-posts", action="append", default=[])
    args = parser.parse_args(argv)
    if args.max_pages < 0:
        parser.error("--max-pages must be >= 0")
    load_dotenv(Path(args.project) / ".env")
    configuration = json.loads(Path(args.accounts).read_text(encoding="utf-8"))
    archive = SocialArchive(args.directory)
    clients = []
    try:
        manifest_path = archive.directory / "plan.json"
        saved = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        end = stamp(args.end or saved.get("end") or (datetime.now(timezone.utc) - timedelta(seconds=60)))
        if end > stamp(datetime.now(timezone.utc)):
            parser.error("--end cannot be in the future")
        manifest_path.write_text(encoded({"end": end, "months": args.months, "end_exclusive": True}), encoding="utf-8")
        inventory = archive.inventory(args.project)
        for path in args.import_posts:
            inventory["sources"].append(archive.import_posts(path))
        inventory["existing_post_ids_hash"] = archive.index_digest()
        inventory["existing_id_count"] = archive.db.execute("SELECT COUNT(*) FROM existing_ids").fetchone()[0]
        (archive.directory / "inventory.json").write_text(encoded(inventory), encoding="utf-8")
        adapters = {}
        if args.run and inventory["ready"]:
            from parsers.social_api import SocialAPI
            for platform, key in TOKEN_KEYS.items():
                token = os.environ.get(key)
                if token:
                    client = httpx.Client(timeout=30, follow_redirects=False)
                    clients.append(client)
                    adapters[platform] = SocialAPI(platform, token, client=client)
                    adapters[platform].existing_id_lookup = archive.contains
        prepare(archive, configuration, end, inventory["existing_post_ids_hash"], adapters, args.months,
                block_reason=None if inventory["ready"] else "Project inventory incomplete; inspect inventory.json")
        if args.retry_failed:
            with archive.db:
                archive.db.execute("UPDATE jobs SET status='pending' WHERE status IN ('failed','paused_duplicates_incomplete')")
        attempted = run(archive, adapters, args.max_pages) if args.run and inventory["ready"] else 0
        report = archive.export(inventory, configuration.get("unresolved", []))
        report["page_requests_attempted_this_run"] = attempted
        (archive.directory / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_report(archive, report)
        print(json.dumps({key: report[key] for key in ("new_posts", "existing_id_count", "job_count", "jobs_by_status", "page_requests_attempted_this_run", "completeness")}, ensure_ascii=False, indent=2))
        return 0 if report["all_windows_api_exhausted"] else 2
    finally:
        for client in clients:
            client.close()
        archive.close()


if __name__ == "__main__":
    raise SystemExit(main())
