"""Tests for configurable secondary Steam sessions (STEAM_ALT_SESSIONS)."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from git_logger.config import Settings
from git_logger.state import StateStore
from git_logger.tracker import Tracker

STEAM_URL = "https://steamcommunity.com/id/example/games/?tab=all"
ENV_KEY = "STEAM_ALT_SESSIONS"


def _settings_with(raw):
    if raw is None:
        os.environ.pop(ENV_KEY, None)
    else:
        os.environ[ENV_KEY] = raw
    return Settings.load(env_file="/nonexistent/.env")


def _validate_joined(settings):
    return " | ".join(settings.validate())


class AltSessionConfigTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(ENV_KEY, None)

    def test_unset_env_yields_no_sessions(self):
        settings = _settings_with(None)
        self.assertEqual(settings.alt_steam_sessions, ())
        self.assertEqual(settings.validate(), [])

    def test_valid_json_is_parsed(self):
        raw = (
            '[{"name":"alt","username":"second_account",'
            '"cookie_file":"/app/data/alt_cookies.pkl","token_file":"/app/data/alt_tokens.json"}]'
        )
        settings = _settings_with(raw)
        self.assertEqual(settings.validate(), [])
        (spec,) = settings.alt_steam_sessions
        self.assertEqual(spec.name, "alt")
        self.assertEqual(spec.username, "second_account")
        self.assertEqual(spec.cookie_file, Path("/app/data/alt_cookies.pkl"))
        self.assertEqual(spec.token_file, Path("/app/data/alt_tokens.json"))
        self.assertEqual(spec.password, "")

    def test_paths_default_to_data_dir(self):
        settings = _settings_with('[{"name":"alt","username":"u2"}]')
        (spec,) = settings.alt_steam_sessions
        self.assertEqual(spec.cookie_file, settings.data_dir / "steamcookies.pkl")
        self.assertEqual(spec.token_file, settings.data_dir / "steam_tokens.json")

    def test_invalid_json_is_a_validation_error(self):
        self.assertIn("STEAM_ALT_SESSIONS is not valid JSON", _validate_joined(_settings_with("{not json")))

    def test_not_a_list_is_a_validation_error(self):
        self.assertIn("must be a JSON list", _validate_joined(_settings_with('{"name":"alt"}')))

    def test_missing_name_or_username_is_a_validation_error(self):
        errors = _validate_joined(_settings_with('[{"name":"alt"}]')) + " | " + _validate_joined(
            _settings_with('[{"username":"u2"}]')
        )
        self.assertIn("requires 'name' and 'username'", errors)

    def test_duplicate_names_are_a_validation_error(self):
        raw = '[{"name":"a","username":"u1"},{"name":"a","username":"u2"}]'
        self.assertIn("duplicate session names", _validate_joined(_settings_with(raw)))


class _FakeSession:
    def __init__(self, username="x", authed=True, fresh=True):
        self.username = username
        self._authed = authed
        self._fresh = fresh
        self.refreshes = 0

    @property
    def is_authenticated(self):
        return self._authed

    def session_fresh(self):
        return self._fresh

    async def refresh_async(self):
        self.refreshes += 1
        return True

    async def fetch_text(self, url):
        return "FAKE-CONTENT", ""


def _tracker(main, alts):
    return Tracker(
        state=object(),
        repo=object(),
        session=None,
        default_interval=3600,
        steam_session=main,
        alt_sessions={name: s for name, s in alts},
    )


class SessionRoutingTests(unittest.TestCase):
    def test_entry_without_session_uses_default(self):
        main, alt = _FakeSession("main"), _FakeSession("alt")
        tracker = _tracker(main, [("alt", alt)])
        self.assertIs(tracker._steam_session_for({"name": "x"}), main)
        self.assertIs(tracker._steam_session_for({"name": "x", "session": ""}), main)

    def test_named_session_is_used(self):
        main, alt = _FakeSession("main"), _FakeSession("alt")
        tracker = _tracker(main, [("alt", alt), ("other", _FakeSession("other"))])
        self.assertIs(tracker._steam_session_for({"name": "x", "session": "alt"}), alt)

    def test_unknown_session_falls_back_to_default(self):
        main = _FakeSession("main")
        tracker = _tracker(main, [("alt", _FakeSession("alt"))])
        self.assertIs(tracker._steam_session_for({"name": "x", "session": "ghost"}), main)

    def test_fetch_with_named_session_skips_default(self):
        main, alt = _FakeSession("main"), _FakeSession("alt")
        tracker = _tracker(main, [("alt", alt)])
        entry = {"name": "x", "session": "alt"}
        content, error = asyncio.run(tracker._fetch(STEAM_URL, tracker._steam_session_for(entry)))
        self.assertEqual((content, error), ("FAKE-CONTENT", ""))
        self.assertEqual(main.refreshes, 0)


class KeepAliveTests(unittest.TestCase):
    def test_only_authenticated_sessions_get_refreshed(self):
        main = _FakeSession("main", authed=True, fresh=False)
        alt_ok = _FakeSession("alt", authed=True, fresh=False)
        alt_dead = _FakeSession("dead", authed=False, fresh=False)
        tracker = _tracker(main, [("alt", alt_ok), ("dead", alt_dead)])
        asyncio.run(tracker._ensure_steam_fresh())
        self.assertEqual(main.refreshes, 1)
        self.assertEqual(alt_ok.refreshes, 1)
        self.assertEqual(alt_dead.refreshes, 0)

    def test_fresh_sessions_are_left_alone(self):
        main = _FakeSession("main", authed=True, fresh=True)
        alt = _FakeSession("alt", authed=True, fresh=True)
        tracker = _tracker(main, [("alt", alt)])
        asyncio.run(tracker._ensure_steam_fresh())
        self.assertEqual(main.refreshes, 0)
        self.assertEqual(alt.refreshes, 0)


class StateSessionFieldTests(unittest.TestCase):
    def test_add_url_defaults_to_no_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.json")
            entry = state.add_url("https://example.com/a", name="Example")
            self.assertEqual(entry["session"], "")

    def test_update_url_accepts_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.json")
            entry = state.add_url("https://example.com/a", name="Example")
            self.assertTrue(state.update_url(entry["id"], session="alt"))
            self.assertEqual(state.get_url(entry["id"])["session"], "alt")
            self.assertTrue(state.update_url(entry["id"], session=""))
            self.assertEqual(state.get_url(entry["id"])["session"], "")


if __name__ == "__main__":
    unittest.main()
