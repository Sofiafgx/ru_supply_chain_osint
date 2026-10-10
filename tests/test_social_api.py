import unittest

import httpx

from parsers.social_api import CursorExpired, SocialAPI, SourceBlocked


def task(platform="X"):
    return {"platform": platform, "account_handle": "@example", "account_id": "123" if platform == "X" else None,
            "date_range": {"start": "2023-04-01T00:00:00Z", "end": "2023-07-01T00:00:00Z"}}


def xpost(identifier="100", timestamp="2023-04-01T00:00:00Z"):
    return {"id": identifier, "author_id": "123", "text": "A public post", "created_at": timestamp}


def thread(identifier="100", timestamp="2023-04-01T00:00:00+0000"):
    return {"id": identifier, "username": "example", "text": "A public thread", "timestamp": timestamp,
            "permalink": "https://www.threads.net/@example/post/abc"}


class SocialAPITests(unittest.TestCase):
    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def adapter(self, platform, payload, status=200):
        return SocialAPI(platform, "test-secret", self.client(lambda req: httpx.Response(status, json=payload)))

    def test_missing_credentials_make_no_network_request(self):
        called = []
        client = self.client(lambda request: called.append(request))
        for platform in ("X", "Threads"):
            with self.assertRaisesRegex(SourceBlocked, "Missing"):
                SocialAPI(platform, "", client)
        self.assertEqual(called, [])

    def test_x_lookup_preserves_stable_identity_and_creation_date(self):
        def handler(request):
            self.assertEqual(request.url.path, "/2/users/by/username/example")
            self.assertIn("created_at", request.url.params["user.fields"])
            return httpx.Response(200, json={"data": {"id": "123", "username": "Example",
                                                     "created_at": "2009-01-05T12:00:00.000Z"}})
        result = SocialAPI("X", "test", self.client(handler)).resolve_account("@example")
        self.assertEqual(result, {"account_id": "123", "account_handle": "@Example",
                                  "created_at": "2009-01-05T12:00:00Z"})

    def test_threads_lookup_does_not_invent_creation_date_or_user_id(self):
        result = self.adapter("Threads", {"username": "example", "follower_count": 100}).resolve_account("example")
        self.assertIsNone(result["created_at"])
        self.assertIsNone(result["account_id"])

    def test_full_archive_request_has_no_reply_or_repost_exclusions(self):
        def handler(request):
            self.assertEqual(request.url.host, "api.x.com")
            self.assertEqual(request.url.path, "/2/tweets/search/all")
            self.assertEqual(request.url.params["query"], "from:example")
            self.assertIn("note_post", request.url.params["post.fields"])
            self.assertEqual(request.url.params["expansions"], "author_id")
            self.assertEqual(request.url.params["next_token"], "cursor1")
            self.assertEqual(request.url.params["end_time"], task()["date_range"]["end"])
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            return httpx.Response(200, json={"data": [xpost()], "meta": {"result_count": 1, "next_token": "cursor2"}})
        result = SocialAPI("X", "test-secret", self.client(handler)).fetch_page(task(), "cursor1")
        self.assertEqual(result["next_cursor"], "cursor2")
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["posts"][0]["post_id"], "100")
        self.assertEqual(result["coverage_kind"], "full_archive")

    def test_half_open_window_filters_boundary_without_losing_pagination(self):
        rows = [xpost("1", "2023-03-31T23:59:59Z"), xpost("2"), xpost("3", "2023-07-01T00:00:00Z")]
        result = self.adapter("X", {"data": rows, "meta": {"result_count": 3, "next_token": "next"}}).fetch_page(task())
        self.assertEqual([post["post_id"] for post in result["posts"]], ["2"])
        self.assertEqual(result["next_cursor"], "next")

    def test_long_post_text_and_raw_attachments_are_preserved(self):
        for field in ("note_post", "note_tweet"):
            row = dict(xpost(), **{field: {"text": "Complete long post"}, "attachments": {"media_keys": ["media1"]}})
            result = self.adapter("X", {"data": [row], "meta": {"result_count": 1}}).fetch_page(task())
            self.assertEqual(result["posts"][0]["text"], "Complete long post")
            self.assertEqual(result["posts"][0]["raw"]["attachments"]["media_keys"], ["media1"])

    def test_partial_response_is_never_accepted_or_marked_exhausted(self):
        payload = {"data": [xpost()], "meta": {"result_count": 1}, "errors": [{"detail": "partial error"}]}
        with self.assertRaises(SourceBlocked):
            self.adapter("X", payload).fetch_page(task())

    def test_missing_data_or_metadata_is_not_empty_history(self):
        for payload in ({}, {"meta": {"result_count": 1}}, {"data": []}, {"data": [], "meta": {"result_count": 4}}):
            with self.subTest(payload=payload), self.assertRaises(SourceBlocked):
                self.adapter("X", payload).fetch_page(task())
        result = self.adapter("X", {"meta": {"result_count": 0}}).fetch_page(task())
        self.assertTrue(result["exhausted"])

    def test_malformed_post_is_not_silently_dropped(self):
        for post in ({"id": "1"}, dict(xpost(), author_id="wrong"), dict(xpost(), created_at="nonsense")):
            with self.subTest(post=post), self.assertRaises(SourceBlocked):
                self.adapter("X", {"data": [post], "meta": {"result_count": 1}}).fetch_page(task())

    def test_existing_ids_skip_content_but_still_require_valid_identity_and_date(self):
        for platform, row in (("X", xpost()), ("Threads", thread())):
            row["text"] = {"malformed": "must not be inspected for existing posts"}
            payload = {"data": [row], "meta": {"result_count": 1}}
            api = self.adapter(platform, payload)
            calls = []
            api.existing_id_lookup = lambda source, identifier: calls.append((source, identifier)) or True
            result = api.fetch_page(task(platform))
            self.assertEqual(calls, [(platform, "100")])
            self.assertEqual(result["posts"][0]["status"], "SKIPPED_EXISTING")
            self.assertEqual(result["posts"][0]["text"], "")
            self.assertNotIn("raw", result["posts"][0])
            row.pop("created_at" if platform == "X" else "timestamp")
            with self.assertRaises(SourceBlocked):
                api.fetch_page(task(platform))

    def test_known_duplicate_at_exclusive_end_stays_outside_window(self):
        row = xpost(timestamp="2023-07-01T00:00:00Z")
        row["text"] = None
        api = self.adapter("X", {"data": [row], "meta": {"result_count": 1}})
        api.existing_id_lookup = lambda platform, identifier: True
        self.assertEqual(api.fetch_page(task())["posts"], [])

    def test_cursor_rejection_is_distinguished_from_access_token_failure(self):
        adapter = self.adapter("X", {"errors": [{"detail": "Invalid next_token: test-secret"}]}, 400)
        with self.assertRaises(CursorExpired) as caught:
            adapter.fetch_page(task(), "expired")
        self.assertNotIn("test-secret", str(caught.exception))
        with self.assertRaises(SourceBlocked) as caught:
            self.adapter("Threads", {"error": {"message": "Access token invalid test-secret"}}, 401).fetch_page(task("Threads"), "c")
        self.assertNotIsInstance(caught.exception, CursorExpired)
        self.assertNotIn("test-secret", str(caught.exception))

    def test_repeated_cursor_is_a_block_not_an_infinite_loop(self):
        with self.assertRaisesRegex(SourceBlocked, "repeated"):
            self.adapter("X", {"data": [xpost()], "meta": {"result_count": 1, "next_token": "same"}}).fetch_page(task(), "same")

    def test_threads_cursor_only_pagination_and_empty_terminal_page(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1.0/profile_posts")
            self.assertEqual(request.url.params["username"], "example")
            self.assertIn("since", request.url.params)
            self.assertNotIn("owner", request.url.params["fields"])
            if request.url.params.get("after"):
                return httpx.Response(200, json={"data": [], "paging": {"cursors": {"after": "last"}}})
            return httpx.Response(200, json={"data": [thread()], "paging": {"cursors": {"after": "last"}}})
        api = SocialAPI("Threads", "test", self.client(handler))
        first = api.fetch_page(task("Threads"))
        self.assertFalse(first["exhausted"])
        self.assertEqual(first["coverage_kind"], "threads_public_profile_posts")
        self.assertEqual(first["posts"][0]["author_id"], "threads:username:example")
        self.assertTrue(api.fetch_page(task("Threads"), first["next_cursor"])["exhausted"])

    def test_pagination_url_never_controls_destination_or_credentials(self):
        payload = {"data": [thread()], "paging": {"next": "https://evil.example/profile_posts?after=next&access_token=stolen"}}
        with self.assertRaisesRegex(SourceBlocked, "unexpected pagination"):
            self.adapter("Threads", payload).fetch_page(task("Threads"))
        payload["paging"]["next"] = "https://graph.threads.net/v1.0/profile_posts?after=next&access_token=stolen"
        result = self.adapter("Threads", payload).fetch_page(task("Threads"))
        self.assertEqual(result["next_cursor"], "next")
        self.assertNotIn("stolen", str(result))

    def test_no_automatic_retries_or_redirects(self):
        for status in (302, 402, 429, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"error": "test-secret"}, headers={"location": "https://evil.example"})
            with self.assertRaises(SourceBlocked) as caught:
                SocialAPI("X", "test-secret", self.client(handler)).fetch_page(task())
            self.assertEqual(len(calls), 1)
            self.assertNotIn("test-secret", str(caught.exception))

    def test_transport_exception_does_not_leak_tokens(self):
        def handler(request):
            raise httpx.ConnectError("Secret test-secret failed", request=request)
        with self.assertRaises(SourceBlocked) as caught:
            SocialAPI("X", "test-secret", self.client(handler)).fetch_page(task())
        self.assertNotIn("test-secret", str(caught.exception))

    def test_display_name_cannot_inject_search_query(self):
        with self.assertRaisesRegex(SourceBlocked, "Invalid account"):
            self.adapter("X", {}).resolve_account("example OR from:other")


if __name__ == "__main__":
    unittest.main()
