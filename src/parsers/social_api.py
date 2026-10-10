"""Bounded, read-only official API adapters; exhaustion is not proof of all history.

Contracts checked 2026-10-08:
https://docs.x.com/x-api/posts/search/quickstart/full-archive-search
https://docs.x.com/x-api/posts/search-all-posts.md
https://docs.x.com/x-api/posts/search/integrate/operators
https://docs.x.com/x-api/users/get-user-by-username
https://developers.facebook.com/documentation/threads/threads-profiles.md
https://developers.facebook.com/documentation/threads/retrieve-and-discover-posts/retrieve-posts.md

Threads public discovery needs advanced access for non-Meta profiles, requires
100+ followers and has a 1,000 requests / rolling 24h limit. It does not expose
account creation dates or public user IDs, and replies use a different API.
Each method performs at most one request: a caller controls budgets and retries.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx


class SourceBlocked(RuntimeError):
    """Collection cannot advance; the caller must preserve its checkpoint."""


class CursorExpired(SourceBlocked):
    """The source explicitly rejected a pagination cursor, requiring replay."""


def _date(value):
    try:
        value = value.replace("Z", "+00:00")
        # Python 3.9 fromisoformat does not accept Meta's +0000 offset.
        value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        return parsed.astimezone(timezone.utc)
    except (TypeError, AttributeError, ValueError):
        raise SourceBlocked("A valid timezone-aware ISO timestamp is required.") from None


def _iso(value):
    return _date(value).isoformat().replace("+00:00", "Z")


class SocialAPI:
    def __init__(self, platform: str, token: Optional[str], client=None, existing_id_lookup=None):
        aliases = {"x": "X", "twitter": "X", "threads": "Threads"}
        self.platform = aliases.get(str(platform).lower())
        if self.platform is None:
            raise SourceBlocked("Unsupported platform; expected X or Threads.")
        if not isinstance(token, str) or not token.strip():
            name = "X_BEARER_TOKEN" if self.platform == "X" else "THREADS_ACCESS_TOKEN"
            raise SourceBlocked("Missing %s; configure official API access before collecting." % name)
        self._token = token.strip()
        # Exact project-index callback (platform, post_id) -> bool; never a Bloom
        # filter alone, whose false positives could discard uncollected posts.
        self.existing_id_lookup = existing_id_lookup
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=False)

    def close(self):
        if self._owns_client:
            self._client.close()

    def _handle(self, handle):
        if not isinstance(handle, str):
            raise SourceBlocked("An exact account handle is required.")
        handle = handle.lstrip("@")
        pattern = r"[A-Za-z0-9_]{1,15}" if self.platform == "X" else r"[A-Za-z0-9_.]{1,30}"
        if not re.fullmatch(pattern, handle):
            raise SourceBlocked("Invalid account handle; use a username, not a display name or URL.")
        return handle

    def _get(self, path, params, cursor=None):
        origin = "https://api.x.com" if self.platform == "X" else "https://graph.threads.net"
        headers = {"Authorization": "Bearer " + self._token}
        try:
            response = self._client.get(origin + path, params=params, headers=headers,
                                        timeout=30.0, follow_redirects=False)
        except httpx.HTTPError:
            # Do not include the transport exception: it may embed credentials/URLs.
            raise SourceBlocked("API transport failure; checkpoint retained. Retry explicitly later.") from None
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise SourceBlocked("API returned non-JSON data (HTTP %s)." % response.status_code) from None
        if not isinstance(payload, dict):
            raise SourceBlocked("API response must be a JSON object; checkpoint retained.")
        errors = payload.get("errors") or payload.get("error")
        if response.status_code >= 300 or errors:
            # Inspect an error locally, but never return an upstream body or token.
            diagnostic = str(errors or payload).lower()
            bad_cursor = any(word in diagnostic for word in ("cursor", "next_token", "pagination_token"))
            invalid = any(word in diagnostic for word in ("invalid", "expired", "not valid"))
            if cursor and response.status_code in (200, 400) and bad_cursor and invalid:
                raise CursorExpired("API rejected the saved cursor; replay this window with deduplication.")
            if response.status_code == 429:
                raise SourceBlocked("API rate limit reached; checkpoint retained. Retry after the provider reset.")
            if response.status_code in (401, 403):
                scope = ("full-archive search access" if self.platform == "X" else
                         "threads_basic and advanced threads_profile_discovery access")
                raise SourceBlocked("API access denied; verify token and %s." % scope)
            if response.status_code == 402:
                raise SourceBlocked("API usage credits unavailable; no automatic purchase or retry.")
            raise SourceBlocked("API rejected or partially failed the request (HTTP %s); checkpoint retained."
                                % response.status_code)
        return payload

    def resolve_account(self, handle: str) -> dict:
        handle = self._handle(handle)
        if self.platform == "X":
            payload = self._get("/2/users/by/username/" + handle,
                                {"user.fields": "created_at,protected"})
            profile = payload.get("data")
            if not isinstance(profile, dict) or not profile.get("id") or not profile.get("created_at"):
                raise SourceBlocked("X profile lookup did not return identity and creation date.")
            if profile.get("protected"):
                raise SourceBlocked("Account is protected; this collector handles public posts only.")
            account_id = str(profile["id"])
            created_at = _iso(profile["created_at"])
        else:
            profile = self._get("/v1.0/profile_lookup", {"username": handle})
            account_id, created_at = None, None
            followers = profile.get("follower_count")
            if isinstance(followers, int) and followers < 100:
                raise SourceBlocked("Threads public profile API requires at least 100 followers.")
        returned = profile.get("username")
        if not isinstance(returned, str) or returned.lower() != handle.lower():
            raise SourceBlocked("Profile lookup did not return the exact requested username.")
        return {"account_id": account_id, "account_handle": "@" + returned,
                "created_at": created_at}

    def fetch_page(self, task: dict, cursor: Optional[str] = None) -> dict:
        handle = self._handle(task.get("account_handle"))
        if str(task.get("platform", self.platform)).lower() not in (
                self.platform.lower(), "twitter" if self.platform == "X" else "threads"):
            raise SourceBlocked("Task platform does not match adapter.")
        window = task.get("date_range") or {}
        start, end = _date(window.get("start")), _date(window.get("end"))
        if start >= end:
            raise SourceBlocked("date_range must be a nonempty half-open [start, end) interval.")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise SourceBlocked("Invalid pagination cursor.")
        if self.platform == "X":
            params = {"query": "from:" + handle, "start_time": _iso(window["start"]),
                      "end_time": _iso(window["end"]), "max_results": 100,
                      "sort_order": "recency",
                      "post.fields": "created_at,text,note_post,attachments",
                      "expansions": "author_id"}
            if cursor:
                params["next_token"] = cursor
            payload = self._get("/2/tweets/search/all", params, cursor)
            meta = payload.get("meta")
            if not isinstance(meta, dict) or not isinstance(meta.get("result_count"), int):
                raise SourceBlocked("X search response is missing result metadata.")
            rows = payload.get("data", [] if meta["result_count"] == 0 else None)
            next_cursor = meta.get("next_token")
            coverage = "full_archive"
        else:
            params = {"username": handle, "since": _iso(window["start"]),
                      "until": _iso(window["end"]), "limit": 100,
                      "fields": "id,username,text,timestamp,permalink,media_type,shortcode"}
            if cursor:
                params["after"] = cursor
            payload = self._get("/v1.0/profile_posts", params, cursor)
            rows = payload.get("data")
            next_cursor = self._threads_cursor(payload, rows)
            coverage = "threads_public_profile_posts"
        if not isinstance(rows, list):
            raise SourceBlocked("API response is missing a post list; checkpoint retained.")
        if self.platform == "X" and meta["result_count"] != len(rows):
            raise SourceBlocked("X result count differs from returned posts; checkpoint retained.")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
            raise SourceBlocked("API returned an invalid next cursor.")
        if next_cursor and next_cursor == cursor:
            raise SourceBlocked("API repeated its pagination cursor; checkpoint retained.")
        posts = []
        for row in rows:
            post = self._post(row, task, handle)
            if start <= _date(post["timestamp"]) < end:
                posts.append(post)
        return {"posts": posts, "next_cursor": next_cursor,
                "exhausted": next_cursor is None, "coverage_kind": coverage}

    @staticmethod
    def _threads_cursor(payload, rows):
        paging = payload.get("paging", {})
        if not isinstance(paging, dict) or not isinstance(paging.get("cursors", {}), dict):
            raise SourceBlocked("Threads returned malformed pagination.")
        candidate = paging.get("cursors", {}).get("after")
        link = paging.get("next")
        if link:
            if not isinstance(link, str):
                raise SourceBlocked("Threads returned malformed next-page URL.")
            parsed = urlparse(link)
            if parsed.scheme != "https" or parsed.netloc != "graph.threads.net" or parsed.path not in (
                    "/profile_posts", "/v1.0/profile_posts"):
                raise SourceBlocked("Threads returned an unexpected pagination origin or endpoint.")
            values = parse_qs(parsed.query).get("after", [])
            if len(values) != 1 or (candidate and candidate != values[0]):
                raise SourceBlocked("Threads pagination cursor is missing or inconsistent.")
            candidate = values[0]
        # Meta examples contain only cursors. Follow after to an empty page when
        # necessary rather than treating missing paging.next as proof of exhaustion.
        if rows == [] and not link:
            return None
        return candidate

    def _post(self, row, task, handle):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise SourceBlocked("Post has no valid source ID; checkpoint retained.")
        if self.platform == "X":
            author_id = row.get("author_id")
            if not isinstance(author_id, str) or not author_id:
                raise SourceBlocked("X post is missing its author ID.")
            if task.get("account_id") and str(task["account_id"]) != author_id:
                raise SourceBlocked("X returned a different author; verify a possible handle change.")
            timestamp = _iso(row.get("created_at"))
            url = "https://x.com/%s/status/%s" % (handle, row["id"])
        else:
            username = row.get("username")
            if not isinstance(username, str) or username.lower() != handle.lower():
                raise SourceBlocked("Threads returned a different or unknown author.")
            # Explicit namespace: this is a handle identity, not a Meta user ID.
            author_id = "threads:username:" + handle.lower()
            timestamp = _iso(row.get("timestamp"))
            url = row.get("permalink") or ""
        post = {"platform": self.platform, "post_id": row["id"], "author_id": author_id,
                "account_handle": "@" + handle, "text": "", "timestamp": timestamp, "url": url}
        if self.existing_id_lookup and self.existing_id_lookup(self.platform, row["id"]):
            # Keep identity/time for range validation and duplicate streaks, but
            # do not inspect content or return raw data that could be re-saved.
            post["status"] = "SKIPPED_EXISTING"
            return post
        if self.platform == "X":
            note = row.get("note_tweet") or row.get("note_post")
            text = note.get("text", row.get("text")) if isinstance(note, dict) else row.get("text")
        else:
            text = row.get("text", "")
        if not isinstance(text, str):
            raise SourceBlocked("Post text is malformed; checkpoint retained.")
        post.update(text=text, raw=row)
        return post
