"""Tests for automatic Steam session recovery.

Regression (2026-11-23): after a host reboot the container started before
the Pi's networking was up. Startup auth then failed with a DNS error, and
the session stayed unauthenticated for the rest of the machine's uptime
because nothing re-tried the (still valid) refresh token. The session must
heal itself from the saved refresh token once connectivity is back.
"""

import json
import unittest
from unittest import mock

from git_logger.steam_session import SteamSession

GAMES_URL = "https://steamcommunity.com/profiles/76561198111222333/games/?tab=all"


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
        "</body></html>"
    )
    return page


def _owned_games_query(games, steamid="76561198111222333", language="english"):
    return {
        "state": {"data": games, "status": "success", "fetchStatus": "idle"},
        "queryKey": ["OwnedGames", steamid, language],
        "queryHash": json.dumps(["OwnedGames", steamid, language]),
    }


def _games_page(games):
    return _ssr_page([_owned_games_query(games)])


class _FakeAuth:
    def __init__(self, result=True):
        self.result = result
        self.calls = 0

    def refresh_authenticated_session(self):
        self.calls += 1
        return self.result


class _FakeResponse:
    def __init__(self, text, url):
        self.status_code = 200
        self.text = text
        self.url = url


class _FakeRequestsSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.cookies = []

    def get(self, url, allow_redirects=True, timeout=None):
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse("<html>unexpected</html>", url)


class SessionRecoveryTests(unittest.TestCase):
    def _session(self, authenticated, auth):
        session = SteamSession("user", "pass", None, None)
        session._authenticated = authenticated
        session._authenticator = auth
        return session

    def test_recovers_from_unauthenticated_start(self):
        """The 11-23 incident: auth failed at boot, network comes back, the
        next fetch must re-establish the session from the saved token."""
        auth = _FakeAuth(result=True)
        session = self._session(False, auth)
        games = [{"appid": 570, "name": "Dota 2"}, {"appid": 10, "name": "Counter-Strike"}]
        session.session = _FakeRequestsSession(
            _FakeResponse(_games_page(games), GAMES_URL + "&l=english")
        )

        content, error = session.fetch_text_sync(GAMES_URL)

        self.assertEqual(error, "")
        self.assertEqual(json.loads(content), [
            {"appid": 10, "name": "Counter-Strike"},
            {"appid": 570, "name": "Dota 2"},
        ])
        self.assertEqual(auth.calls, 1)
        self.assertTrue(session._authenticated)

    def test_recovery_is_rate_limited(self):
        """A dead token or dead network must not turn every fetch into a
        fresh auth round-trip; attempts are spaced by the cooldown."""
        auth = _FakeAuth(result=False)
        session = self._session(False, auth)
        session.session = _FakeRequestsSession()

        _, first = session.fetch_text_sync(GAMES_URL)
        _, second = session.fetch_text_sync(GAMES_URL)

        self.assertEqual(first, "Steam not authenticated")
        self.assertEqual(second, "Steam not authenticated")
        self.assertEqual(auth.calls, 1)

    def test_login_wall_mid_fetch_triggers_refresh_and_retry(self):
        auth = _FakeAuth(result=True)
        session = self._session(True, auth)
        games = [{"appid": 10, "name": "Counter-Strike"}]
        login_wall = _FakeResponse("<html>Sign in</html>", "https://steamcommunity.com/login/home/")
        good = _FakeResponse(_games_page(games), GAMES_URL + "&l=english")
        session.session = _FakeRequestsSession(login_wall, good)

        content, error = session.fetch_text_sync(GAMES_URL)

        self.assertEqual(error, "")
        self.assertEqual(json.loads(content), [{"appid": 10, "name": "Counter-Strike"}])
        self.assertEqual(auth.calls, 1)

    def test_no_authenticator_falls_back_to_full_reauth_once(self):
        session = self._session(False, None)
        session.session = _FakeRequestsSession()
        with mock.patch.object(SteamSession, "ensure_authenticated", return_value=False) as reauth:
            _, first = session.fetch_text_sync(GAMES_URL)
            _, second = session.fetch_text_sync(GAMES_URL)
        self.assertEqual(first, "Steam not authenticated")
        self.assertEqual(second, "Steam not authenticated")
        self.assertEqual(reauth.call_count, 1)
        reauth.assert_called_once_with(interactive=False)


class EnsureSteamFreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthenticated_session_is_recovered_in_tracker_loop(self):
        """The tracker's 30s maintenance loop must drive recovery for a
        session that never authenticated (e.g. container started before the
        host network was up), instead of waiting for the next fetch."""
        from git_logger.tracker import Tracker

        auth = _FakeAuth(result=True)
        session = SteamSession("user", "pass", None, None)
        session._authenticated = False
        session._authenticator = auth

        class _NullRepo:
            pass

        tracker = Tracker(state=None, repo=_NullRepo(), session=None, default_interval=3600,
                          steam_session=session, alt_sessions={})
        await tracker._ensure_steam_fresh()

        self.assertEqual(auth.calls, 1)
        self.assertTrue(session._authenticated)

    async def test_authenticated_fresh_session_is_untouched(self):
        from git_logger.tracker import Tracker

        session = SteamSession("user", "pass", None, None)
        session._authenticated = True
        session.session = _FakeRequestsSession()

        class _NullRepo:
            pass

        tracker = Tracker(state=None, repo=_NullRepo(), session=None, default_interval=3600,
                          steam_session=session, alt_sessions={})
        # session_fresh() is False (no cookie JWT), so the near-expiry branch
        # would refresh; the call must still land on the authenticator.
        with mock.patch.object(SteamSession, "refresh_session", return_value=True) as refresh:
            await tracker._ensure_steam_fresh()
        refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
