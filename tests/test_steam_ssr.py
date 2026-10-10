"""Tests for the Steam games page SSR extraction.

The fixture pages mirror the two page generations git-logger has seen:
the rebuilt page ships its react-query cache inside a plain
<script type="application/json" id="valve-ssr-data"> element whose
renderContext.queryData is a doubly-encoded JSON string.
"""

import json
import unittest

from git_logger.steam_session import SteamSession


def _ssr_page(queries, *, logged_in=True):
    """Build a games page body in the rebuilt valve-ssr-data shape."""
    render_context = {
        "localizationSettings": {"languages": [{"strLanguage": "english"}]},
        "queryData": json.dumps({"mutations": [], "queries": queries}),
    }
    payload = {
        "route": "games",
        "loaderData": {},
        "clientAssets": {},
        "Config": {},
        "UserConfig": {"accountid": 1234 if logged_in else None},
        "renderContext": render_context,
    }
    element = json.dumps(payload)
    page = (
        '<html><head><title>Steam Community :: Games</title></head><body>'
        '<script type="application/json" id="valve-ssr-data">'
        + element
        + "</script>"
        "<script>(()=>{const e=document.getElementById(\"valve-ssr-data\");"
        'const d=JSON.parse(e.textContent);e.remove();'
        "window.SSR={loaderData:d.loaderData,renderContext:d.renderContext};})();</script>"
        "</body></html>"
    )
    return page


def _owned_games_query(games, steamid="76561198061432306", language="english"):
    return {
        "state": {"data": games, "status": "success", "fetchStatus": "idle"},
        "queryKey": ["OwnedGames", steamid, language],
        "queryHash": json.dumps(["OwnedGames", steamid, language]),
    }


class ExtractGameListTests(unittest.TestCase):
    def test_rebuilt_page_yields_sorted_game_list(self):
        games = [
            {"appid": 570, "name": "Dota 2", "playtime_forever": 10},
            {"appid": 10, "name": "Counter-Strike", "playtime_forever": 100},
            {"appid": 730, "name": "Counter-Strike 2", "playtime_forever": 1},
        ]
        page = _ssr_page([
            {"state": {"data": None}, "queryKey": ["AchievementProgress", {"steamid": "x"}]},
            _owned_games_query(games),
            {"state": {"data": [730, 570]}, "queryKey": ["OwnedGameIds", "76561198061432306"]},
        ])
        result = SteamSession._extract_game_list(page)
        self.assertEqual(
            result,
            [
                {"appid": 10, "name": "Counter-Strike"},
                {"appid": 570, "name": "Dota 2"},
                {"appid": 730, "name": "Counter-Strike 2"},
            ],
        )

    def test_entries_without_appid_or_name_are_skipped(self):
        games = [
            {"appid": 440, "name": "Team Fortress 2"},
            {"appid": 999},
            {"name": "No Appid"},
        ]
        page = _ssr_page([_owned_games_query(games)])
        self.assertEqual(SteamSession._extract_game_list(page), [{"appid": 440, "name": "Team Fortress 2"}])

    def test_empty_owned_games_list_yields_none(self):
        page = _ssr_page([_owned_games_query([])])
        self.assertIsNone(SteamSession._extract_game_list(page))

    def test_null_owned_games_data_yields_none(self):
        page = _ssr_page([_owned_games_query(None)])
        self.assertIsNone(SteamSession._extract_game_list(page))

    def test_page_without_ssr_data_yields_none(self):
        self.assertIsNone(SteamSession._extract_game_list("<html><body>Sign In</body></html>"))

    def test_malformed_ssr_json_yields_none(self):
        page = (
            '<html><body><script type="application/json" id="valve-ssr-data">'
            "{not json}"
            "</script></body></html>"
        )
        self.assertIsNone(SteamSession._extract_game_list(page))

    def test_missing_owned_games_query_yields_none(self):
        page = _ssr_page([
            {"state": {"data": [1, 2]}, "queryKey": ["OwnedGameIds", "76561198061432306"]},
        ])
        self.assertIsNone(SteamSession._extract_game_list(page))

    def test_query_data_as_plain_object_is_accepted(self):
        """queryData may arrive already decoded; the parser stays tolerant."""
        render_context = {
            "queryData": {"mutations": [], "queries": [_owned_games_query([{"appid": 10, "name": "Counter-Strike"}])]},
        }
        element = json.dumps({"renderContext": render_context})
        page = (
            '<script type="application/json" id="valve-ssr-data">' + element + "</script>"
        )
        self.assertEqual(
            SteamSession._extract_game_list(page),
            [{"appid": 10, "name": "Counter-Strike"}],
        )

    def test_game_names_with_unicode_survive_the_roundtrip(self):
        games = [{"appid": 241870, "name": "Naruto Shippuden: Ultimate Ninja Storm Revolution"}]
        page = _ssr_page([_owned_games_query(games)])
        self.assertEqual(SteamSession._extract_game_list(page), games)


class ResolveSteamIdSsrTests(unittest.TestCase):
    def _session_with_page(self, page):
        session = SteamSession("user", "pass", None, None)

        class FakeResponse:
            status_code = 200
            text = page

        class FakeRequestsSession:
            def get(self, url, timeout=None):
                return FakeResponse()

        session.session = FakeRequestsSession()
        return session

    def test_steamid_comes_from_the_owned_games_query_key(self):
        page = _ssr_page([_owned_games_query([], steamid="76561198765432109")])
        session = self._session_with_page(page)
        self.assertEqual(session._resolve_steamid_ssr("somevanity"), "76561198765432109")

    def test_page_without_ssr_data_returns_none(self):
        session = self._session_with_page("<html>Sign In</html>")
        self.assertIsNone(session._resolve_steamid_ssr("somevanity"))

    def test_http_error_returns_none(self):
        session = SteamSession("user", "pass", None, None)

        class FakeRequestsSession:
            def get(self, url, timeout=None):
                raise OSError("boom")

        session.session = FakeRequestsSession()
        self.assertIsNone(session._resolve_steamid_ssr("somevanity"))


class LoggedOutDetectionTests(unittest.TestCase):
    def test_new_login_wall_is_detected(self):
        self.assertTrue(SteamSession._looks_logged_out("<html><script>g_steamID = false;</script>"))

    def test_old_encoded_marker_is_still_detected(self):
        self.assertTrue(SteamSession._looks_logged_out('x logged_in&quot;:false y'))

    def test_logged_in_page_is_not_flagged(self):
        self.assertFalse(SteamSession._looks_logged_out('<html><script>g_steamID = "76561198000000000";</script>'))


class FetchTextSyncTests(unittest.TestCase):
    def test_games_page_is_converted_to_json(self):
        session = SteamSession("user", "pass", None, None)
        session._authenticated = True

        games = [
            {"appid": 570, "name": "Dota 2"},
            {"appid": 10, "name": "Counter-Strike"},
        ]
        page = _ssr_page([_owned_games_query(games, steamid="76561198111222333")])

        class FakeResponse:
            status_code = 200
            text = page
            url = "https://steamcommunity.com/profiles/76561198111222333/games/?tab=all&l=english"

        class FakeRequestsSession:
            def get(self, url, allow_redirects=True, timeout=None):
                return FakeResponse()

        session.session = FakeRequestsSession()

        content, error = session.fetch_text_sync(
            "https://steamcommunity.com/profiles/76561198111222333/games/?tab=all"
        )
        self.assertEqual(error, "")
        self.assertEqual(json.loads(content), [
            {"appid": 10, "name": "Counter-Strike"},
            {"appid": 570, "name": "Dota 2"},
        ])

    def test_games_page_without_ssr_data_reports_an_error(self):
        session = SteamSession("user", "pass", None, None)
        session._authenticated = True

        class FakeResponse:
            status_code = 200
            text = "<html>games page with no data</html>"
            url = "https://steamcommunity.com/profiles/76561198111222333/games/?tab=all&l=english"

        class FakeRequestsSession:
            def get(self, url, allow_redirects=True, timeout=None):
                return FakeResponse()

        session.session = FakeRequestsSession()

        content, error = session.fetch_text_sync(
            "https://steamcommunity.com/profiles/76561198111222333/games/?tab=all"
        )
        self.assertEqual(content, "")
        self.assertIn("no SSR data", error)


if __name__ == "__main__":
    unittest.main()
