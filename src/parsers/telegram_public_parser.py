"""Анонімний збір публічних прев'ю t.me/s; окремий від архіву Telegram API."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_public_page(html, username):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one(".tgme_channel_info_header_title")
    records = []
    for widget in soup.select(".tgme_widget_message[data-post]"):
        address = widget["data-post"].split("/")
        if len(address) != 2 or address[0].lower() != username.lower() or not address[1].isdigit():
            continue
        # Прев'ю цитованого повідомлення може стояти перед власним текстом поста.
        body = widget.select_one(".tgme_widget_message_text.js-message_text")
        if body is None:
            body = widget.select_one(".tgme_widget_message_text:not(.js-message_reply_text)")
        reply_preview = widget.select_one(".js-message_reply_text")
        text_html = str(body) if body else None
        links = list(dict.fromkeys(a["href"] for a in body.select("a[href]"))) if body else []
        if body:
            for br in body.select("br"):
                br.replace_with("\n")
        timestamp = widget.select_one("time[datetime]")
        views = widget.select_one(".tgme_widget_message_views")
        forwarded = widget.select_one(".tgme_widget_message_forwarded_from")
        warning = widget.select_one(".tgme_widget_message_error")
        media = []
        for photo in widget.select(".tgme_widget_message_photo_wrap"):
            image = re.search(r"background-image:\s*url\(['\"]?([^)'\"]+)", photo.get("style", ""))
            if image:
                media.append({"type": "photo_preview", "url": urljoin("https://t.me", image[1])})
        for video in widget.select("video"):
            src = video.get("src")
            source = video.select_one("source[src]")
            src = src or (source.get("src") if source else None)
            media.append({"type": "video_preview", "url": urljoin("https://t.me", src) if src else None,
                          "poster": video.get("poster")})
        for document in widget.select(".tgme_widget_message_document_wrap"):
            media.append({"type": "document_preview", "description": document.get_text(" ", strip=True)})
        records.append({
            "source_mode": "public_web", "channel_username": address[0],
            "channel_title": title.get_text(" ", strip=True) if title else username,
            "message_id": int(address[1]), "date": timestamp.get("datetime") if timestamp else None,
            "text": body.get_text().strip() if body else "", "text_html": text_html,
            "reply_preview": reply_preview.get_text(" ", strip=True) if reply_preview else None,
            "url": f"https://t.me/{address[0]}/{address[1]}",
            "links": links, "views_display": views.get_text(strip=True) if views else None,
            "forwarded_from": forwarded.get_text(" ", strip=True) if forwarded else None,
            "media": media, "preview_warning": warning.get_text(" ", strip=True) if warning else None,
            "collected_at": now(),
        })
    records.sort(key=lambda item: item["message_id"])
    # Довіряємо лише посиланню на той самий канал і домен.
    next_before = None
    pagination = soup.select_one("a.tme_messages_more[href]")
    if pagination:
        parsed = urlparse(urljoin("https://t.me", pagination["href"]))
        candidate = parse_qs(parsed.query).get("before", [""])[0]
        if (parsed.scheme == "https" and parsed.netloc == "t.me"
                and parsed.path.lower() == f"/s/{username}".lower() and candidate.isdigit()):
            next_before = int(candidate)
    return records, next_before


class PublicArchive:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS web_sources (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                before_id INTEGER,
                history_done INTEGER NOT NULL DEFAULT 0,
                newest_id INTEGER NOT NULL DEFAULT 0,
                refresh_pending INTEGER NOT NULL DEFAULT 1,
                refresh_before INTEGER,
                refresh_stop INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS web_posts (
                username TEXT COLLATE NOCASE NOT NULL,
                message_id INTEGER NOT NULL,
                date TEXT,
                record_json TEXT NOT NULL,
                PRIMARY KEY(username, message_id)
            );
        """)

    def prepare(self, usernames):
        with self.db:
            for username in usernames:
                self.db.execute("INSERT OR IGNORE INTO web_sources(username) VALUES (?)", (username,))
                self.db.execute("""
                    UPDATE web_sources SET refresh_pending=1,
                        refresh_before=NULL, refresh_stop=newest_id
                    WHERE username=? AND refresh_pending=0
                """, (username,))
                self.db.execute("UPDATE web_sources SET status='pending',error=NULL WHERE username=?", (username,))

    def source(self, username):
        return dict(self.db.execute("SELECT * FROM web_sources WHERE username=?", (username,)).fetchone())

    def failed(self, username, status, error):
        with self.db:
            self.db.execute("UPDATE web_sources SET status=?,error=?,updated_at=? WHERE username=?",
                            (status, error, now(), username))

    def save_page(self, username, records, next_before, state):
        with self.db:
            for record in records:
                self.db.execute("""
                    INSERT INTO web_posts VALUES (?, ?, ?, ?)
                    ON CONFLICT(username, message_id) DO UPDATE SET
                        date=excluded.date,record_json=excluded.record_json
                """, (username, record["message_id"], record["date"], json.dumps(record, ensure_ascii=False)))
            oldest = min((r["message_id"] for r in records), default=None)
            newest = max((r["message_id"] for r in records), default=state["newest_id"])
            if state["refresh_pending"]:
                if state["refresh_stop"] == 0:
                    # Перший знімок. Далі переходимо до старішої історії.
                    self.db.execute("""
                        UPDATE web_sources SET before_id=?,history_done=?,refresh_pending=0,
                            refresh_before=NULL WHERE username=?
                    """, (next_before, int(next_before is None), username))
                else:
                    finished = next_before is None or oldest is None or oldest <= state["refresh_stop"]
                    self.db.execute("""
                        UPDATE web_sources SET refresh_pending=?,refresh_before=? WHERE username=?
                    """, (int(not finished), None if finished else next_before, username))
            else:
                self.db.execute("UPDATE web_sources SET before_id=?,history_done=? WHERE username=?",
                                (next_before, int(next_before is None), username))
            self.db.execute("""
                UPDATE web_sources SET newest_id=MAX(newest_id,?),error=NULL,updated_at=?,
                    status=CASE WHEN history_done=1 AND refresh_pending=0
                        THEN 'web_history_exhausted' ELSE 'collecting' END
                WHERE username=?
            """, (newest, now(), username))

    def report(self):
        result = []
        for row in self.db.execute("""
            SELECT s.*,COUNT(p.message_id) AS posts,MIN(p.message_id) AS oldest_id,
                MAX(p.message_id) AS latest_id,MIN(p.date) AS oldest_date,MAX(p.date) AS latest_date
            FROM web_sources s LEFT JOIN web_posts p ON p.username=s.username
            GROUP BY s.username ORDER BY s.username COLLATE NOCASE
        """):
            source = dict(row)
            if source["status"] == "web_preview_unavailable":
                boundary = "web_unavailable"
            elif source["history_done"] and source["oldest_id"] == 1:
                boundary = "first_id_reached"
            elif source["history_done"]:
                boundary = "oldest_visible_page_reached"
            else:
                boundary = "history_in_progress"
            source.update(history_boundary=boundary, creation_date=None,
                          complete_since_creation_verified=False)
            result.append(source)
        return result

    def export(self, destination, format="jsonl", username=None):
        destination = Path(destination)
        if destination.resolve() in {self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")}:
            raise ValueError("Експорт не може замінювати архів")
        if format not in {"jsonl", "csv"}:
            raise ValueError("Невідомий формат експорту")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fields = ["source_mode", "channel_username", "channel_title", "message_id", "date", "text",
                  "url", "links", "views_display", "forwarded_from", "media", "reply_preview",
                  "quality_warning", "preview_warning", "collected_at"]
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if format == "csv" else "utf-8",
                                             newline="", dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                writer = csv.DictWriter(output, fieldnames=fields) if format == "csv" else None
                if writer:
                    writer.writeheader()
                query = "SELECT record_json FROM web_posts"
                params = ()
                if username is not None:
                    query += " WHERE username=?"
                    params = (username,)
                query += " ORDER BY username,message_id"
                for row in self.db.execute(query, params):
                    if writer:
                        record = json.loads(row[0])
                        flat = {field: record.get(field) for field in fields}
                        for key, value in flat.items():
                            if isinstance(value, (list, dict)):
                                flat[key] = json.dumps(value, ensure_ascii=False)
                            elif isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                                flat[key] = "'" + value
                        writer.writerow(flat)
                    else:
                        output.write(row[0] + "\n")
            temporary.replace(destination)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def snapshot(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.export(folder / "posts.jsonl")
        self.export(folder / "posts.csv", "csv")
        report = self.report()
        progress = {"updated_at": now(), "posts": sum(s["posts"] for s in report), "sources": report}
        temporary = folder / ".progress.tmp"
        temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(folder / "progress.json")
        boundary_labels = {
            "web_unavailable": "Вебісторія недоступна",
            "first_id_reached": "Дійшли до повідомлення №1; видима історія вичерпана",
            "oldest_visible_page_reached": "Найстаріша видима сторінка; давніші пости не підтверджені",
            "history_in_progress": "Довантаження старіших постів триває",
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                         dir=folder, delete=False) as output:
            coverage_temp = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(["Канал", "Записів", "Найстаріший номер", "Найстаріша дата",
                             "Найновіша дата", "Стан", "Перевірена межа історії",
                             "Повнота з дня створення підтверджена"])
            for source in report:
                writer.writerow([source["username"], source["posts"], source["oldest_id"],
                                 source["oldest_date"], source["latest_date"], source["status"],
                                 boundary_labels[source["history_boundary"]], "Ні — доступний лише вебперегляд"])
        coverage_temp.replace(folder / "channel_coverage.csv")
        for source in report:
            if source["posts"]:
                self.export(folder / f"{source['username']}.csv", "csv", username=source["username"])
        return progress

    def close(self):
        self.db.close()


class PublicTelegramParser:
    def __init__(self, client, archive, delay=1.0):
        self.client, self.archive, self.delay = client, archive, delay
        self.retry_at = {}
        self.retry_count = {}
        self.requests_started = 0

    async def fetch(self, username, before):
        for attempt in range(4):
            try:
                self.requests_started += 1
                response = await self.client.get(f"https://t.me/s/{username}",
                                                 params={"before": before} if before else None)
                if response.status_code == 429:
                    raw = response.headers.get("Retry-After", "")
                    wait = int(raw) if raw.isdigit() else min(60, 5 * 2 ** attempt)
                    LOG.warning("Telegram HTTP 429: пауза %s с", wait)
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                return response.text
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if attempt == 3 or (isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500):
                    raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError("Telegram тимчасово обмежив доступ (HTTP 429)")

    async def step(self, username):
        state = self.archive.source(username)
        if state["status"] in {"error", "web_preview_unavailable"}:
            return False
        if state["history_done"] and not state["refresh_pending"]:
            return False
        if asyncio.get_running_loop().time() < self.retry_at.get(username, 0):
            return True
        before = state["refresh_before"] if state["refresh_pending"] else state["before_id"]
        try:
            html = await self.fetch(username, before)
            records, next_before = parse_public_page(html, username)
            if before and not records and not BeautifulSoup(html, "html.parser").select_one(".tgme_channel_history"):
                raise ValueError("Замість історії повернулась інша сторінка; не позначаємо архів завершеним")
            if not records and not before:
                self.archive.failed(username, "web_preview_unavailable",
                                    "Немає публічного вебархіву: бот, група, недоступний канал або порожній архів")
                LOG.warning("@%s: публічний вебархів недоступний", username)
                return False
            if next_before is not None:
                if not records or next_before <= 0 or (before and next_before >= before):
                    raise ValueError("Пагінація не просувається; курсор збережено для повторного запуску")
                if next_before > min(r["message_id"] for r in records):
                    raise ValueError("Некоректна верхня межа наступної сторінки")
            self.archive.save_page(username, records, next_before, state)
            self.retry_at.pop(username, None)
            self.retry_count.pop(username, None)
            LOG.info("@%s: +%s публічних записів, before=%s", username, len(records), next_before)
            return True
        except (httpx.TransportError, RuntimeError) as error:
            return self._retry_later(username, error)
        except httpx.HTTPStatusError as error:
            if error.response.status_code >= 500 or error.response.status_code == 429:
                return self._retry_later(username, error)
            self.archive.failed(username, "error", f"{type(error).__name__}: {error}")
            LOG.error("@%s: %s", username, error)
            return False
        except ValueError as error:
            self.archive.failed(username, "error", f"{type(error).__name__}: {error}")
            LOG.error("@%s: %s", username, error)
            return False

    def _retry_later(self, username, error):
        attempts = self.retry_count.get(username, 0) + 1
        self.retry_count[username] = attempts
        wait = min(300, 30 * 2 ** min(attempts, 4))
        self.retry_at[username] = asyncio.get_running_loop().time() + wait
        self.archive.failed(username, "network_retry", f"{type(error).__name__}: {error}")
        LOG.warning("@%s: тимчасова мережева помилка; повторимо через %s с", username, wait)
        return True

    async def collect(self, sources, folder, max_rounds=None):
        self.archive.prepare(sources)
        rounds = 0
        last_export = 0.0
        export_interval = 60
        try:
            while max_rounds is None or rounds < max_rounds:
                active = False
                round_requests = self.requests_started
                for username in sources:
                    previous_requests = self.requests_started
                    active = await self.step(username) or active
                    current = asyncio.get_running_loop().time()
                    if current - last_export >= export_interval:
                        progress = self.archive.snapshot(folder)
                        LOG.info("Загалом збережено: %s", progress["posts"])
                        last_export = current
                        export_interval = 900 if progress["posts"] > 100000 else 300 if progress["posts"] > 10000 else 60
                    if self.requests_started != previous_requests:
                        await asyncio.sleep(self.delay)
                rounds += 1
                if rounds == 1:
                    progress = self.archive.snapshot(folder)
                    last_export = asyncio.get_running_loop().time()
                total = self.archive.db.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]
                LOG.info("Прохід %s завершено: %s записів", rounds, total)
                if not active:
                    break
                if round_requests == self.requests_started:
                    # Усі активні джерела чекають відновлення мережі. Не запускаємо порожній цикл.
                    next_retry = min(self.retry_at.values(), default=asyncio.get_running_loop().time() + 30)
                    await asyncio.sleep(max(1, min(30, next_retry - asyncio.get_running_loop().time())))
        finally:
            self.archive.snapshot(folder)
        return self.archive.report()
