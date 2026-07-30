import asyncio
import hashlib
import logging
import time
from typing import Optional

import aiohttp

from .repo_manager import RepoManager, _safe_name
from .state import StateStore
from .steam_session import SteamSession

LOG = logging.getLogger(__name__)


class Tracker:
    def __init__(
        self,
        state: StateStore,
        repo: RepoManager,
        session: aiohttp.ClientSession,
        default_interval: int,
        steam_session: SteamSession | None = None,
    ) -> None:
        self.state = state
        self.repo = repo
        self.session = session
        self.default_interval = default_interval
        self.steam_session = steam_session
        self._stop_event = asyncio.Event()

    async def cleanup_stale_files(self) -> None:
        """Remove site files for URLs no longer tracked, and commit deletions."""
        site_dir = self.repo.repo_path / "sites"
        if not site_dir.exists():
            return
        tracked_safe_names = {_safe_name(u["name"]) for u in self.state.urls}
        for f in site_dir.iterdir():
            if not f.is_file():
                continue
            stem = f.stem
            if stem not in tracked_safe_names:
                LOG.info("Removing stale site file: %s", f.name)
                f.unlink()

    async def run(self) -> None:
        LOG.info("Tracker started")
        await self.cleanup_stale_files()
        while not self._stop_event.is_set():
            await self._check_due()
            await asyncio.sleep(30)

    async def stop(self) -> None:
        self._stop_event.set()

    async def _check_due(self) -> None:
        now = time.time()
        for url_entry in self.state.urls:
            if not url_entry["enabled"]:
                continue
            interval = url_entry.get("interval", self.default_interval)
            last_checked = url_entry.get("last_checked", 0.0)
            if now - last_checked < interval:
                continue
            await self._check(url_entry)

    async def _check(self, url_entry: dict) -> None:
        url_id = url_entry["id"]
        name = url_entry["name"]
        url = url_entry["url"]
        old_hash = url_entry.get("last_hash", "")

        content, error = await self._fetch(url)
        if error:
            LOG.warning("Fetch error for %s (%s): %s", name, url, error)
            self.state.record_check(url_id, old_hash, changed=False, error=error)
            return

        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if new_hash == old_hash:
            self.state.record_check(url_id, new_hash, changed=False)
            return

        self.repo.write_content(name, content)
        changed = self.repo.commit("Update: {} - {}".format(name, url))
        if changed:
            self.repo.push()
            LOG.info("Change detected and committed for %s", name)

        self.state.record_check(url_id, new_hash, changed=True)

    async def _fetch(self, url: str) -> tuple:
        if self.steam_session and SteamSession.is_steam_url(url):
            return await self.steam_session.fetch_text(url)
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return "", "HTTP {}".format(resp.status)
                text = await resp.text()
                return text, ""
        except asyncio.TimeoutError:
            return "", "Timeout"
        except aiohttp.ClientError as exc:
            return "", str(exc)
        except Exception as exc:
            return "", str(exc)

    async def check_url_now(self, url_id: int) -> None:
        for url_entry in self.state.urls:
            if url_entry["id"] == url_id:
                await self._check(url_entry)
                return

    async def check_all_now(self) -> None:
        for url_entry in self.state.urls:
            if url_entry["enabled"]:
                await self._check(url_entry)

    async def trigger_add(self, url_entry: dict) -> None:
        await self._check(url_entry)
