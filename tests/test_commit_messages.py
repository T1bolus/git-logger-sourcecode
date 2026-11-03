"""Tests for the site-update commit message builder.

Steam game libraries are tracked as JSON lists of {appid, name}; the commit
message names the games that were added (or removed). Everything else keeps
the generic Update message.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from git_logger.repo_manager import RepoManager, build_commit_message
from git_logger.steam_session import SteamSession
from git_logger.tracker import Tracker
from git_logger.state import StateStore

URL = "https://steamcommunity.com/id/example/games/?tab=all"


def _games_content(*names):
    games = [{"appid": i + 1, "name": n} for i, n in enumerate(names)]
    return json.dumps(games, indent=2, ensure_ascii=False)


class BuildCommitMessageTests(unittest.TestCase):
    def test_added_game_is_listed(self):
        old = _games_content("Dota 2", "Counter-Strike 2")
        new = _games_content("Dota 2", "Counter-Strike 2", "Killing Floor")
        self.assertEqual(build_commit_message("Silas", URL, old, new), "Silas added Killing Floor")

    def test_multiple_added_games_are_comma_separated(self):
        old = _games_content("Dota 2")
        new = _games_content("Dota 2", "Killing Floor", "Portal 2")
        self.assertEqual(
            build_commit_message("Tibolus Games", URL, old, new),
            "Tibolus Games added Killing Floor,Portal 2",
        )

    def test_removed_games_without_additions_are_listed(self):
        old = _games_content("Dota 2", "Killing Floor")
        new = _games_content("Dota 2")
        self.assertEqual(build_commit_message("Silas", URL, old, new), "Silas removed Killing Floor")

    def test_emptied_library_reports_removals(self):
        old = _games_content("Dota 2", "Killing Floor")
        new = _games_content()
        self.assertEqual(build_commit_message("Silas", URL, old, new), "Silas removed Dota 2,Killing Floor")

    def test_added_and_removed_reports_only_additions(self):
        old = _games_content("Dota 2", "Killing Floor")
        new = _games_content("Dota 2", "Portal 2")
        self.assertEqual(build_commit_message("Silas", URL, old, new), "Silas added Portal 2")

    def test_order_only_change_falls_back_to_update(self):
        old = _games_content("Dota 2", "Killing Floor")
        new = _games_content("Killing Floor", "Dota 2")
        self.assertEqual(build_commit_message("Silas", URL, old, new), "Update: Silas - {}".format(URL))

    def test_first_commit_falls_back_to_update(self):
        new = _games_content("Dota 2", "Killing Floor")
        self.assertEqual(build_commit_message("Silas", URL, "", new), "Update: Silas - {}".format(URL))

    def test_html_content_falls_back_to_update(self):
        self.assertEqual(
            build_commit_message("Example", URL, "<html>a</html>", "<html>b</html>"),
            "Update: Example - {}".format(URL),
        )

    def test_entries_without_name_fall_back_to_update(self):
        old = json.dumps([{"appid": 570, "name": "Dota 2"}])
        new = json.dumps([{"appid": 10}])
        self.assertEqual(build_commit_message("Silas", URL, old, new), "Update: Silas - {}".format(URL))

    def test_unicode_game_names_survive(self):
        old = _games_content("Dota 2")
        new = _games_content("Dota 2", "Naruto Shippuden: Ultimate Ninja Storm Revolution")
        self.assertEqual(
            build_commit_message("Silas", URL, old, new),
            "Silas added Naruto Shippuden: Ultimate Ninja Storm Revolution",
        )


class ReadCurrentContentTests(unittest.TestCase):
    def test_roundtrip_and_missing_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = RepoManager(Path(tmp), "u", "e")
            repo.write_content("My Site", _games_content("Dota 2"))
            self.assertEqual(repo.read_current_content("My Site"), _games_content("Dota 2"))
            self.assertEqual(repo.read_current_content("Unknown"), "")


class _FakeSteamSession:
    def __init__(self, pages):
        self.pages = list(pages)

    async def fetch_text(self, url):
        if not self.pages:
            return "", "no more pages"
        return self.pages.pop(0), ""


class TrackerCommitMessageTests(unittest.TestCase):
    def _run_checks(self, repo_path, pages):
        state = StateStore(repo_path / "state.json")
        entry = state.add_url(URL, name="Silas")
        repo = RepoManager(repo_path / "repo", "Git Logger", "logger@example.com")
        repo.ensure_repo()
        tracker = Tracker(
            state, repo, session=None, default_interval=3600, steam_session=_FakeSteamSession(pages)
        )
        for _ in pages:
            asyncio.run(tracker.check_url_now(entry["id"]))
        return repo

    def test_first_check_uses_update_and_later_check_names_added_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._run_checks(
                Path(tmp),
                [
                    _games_content("Dota 2", "Counter-Strike 2"),
                    _games_content("Dota 2", "Counter-Strike 2", "Killing Floor"),
                ],
            )
            log = repo.get_log(2)
            self.assertIn("Update: Silas - {}".format(URL), log)
            self.assertIn("Silas added Killing Floor", log)

    def test_removal_is_committed_with_removed_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._run_checks(
                Path(tmp),
                [
                    _games_content("Dota 2", "Killing Floor"),
                    _games_content("Dota 2"),
                ],
            )
            self.assertIn("Silas removed Killing Floor", repo.get_log(2))

    def test_unchanged_content_creates_no_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._run_checks(Path(tmp), [_games_content("Dota 2"), _games_content("Dota 2")])
            self.assertEqual(repo.get_log(5).count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
