import asyncio
import hashlib
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from .repo_manager import RepoManager, _safe_name, build_commit_message
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
        alt_sessions: Optional[Dict[str, SteamSession]] = None,
    ) -> None:
        self.state = state
        self.repo = repo
        self.session = session
        self.default_interval = default_interval
        self.steam_session = steam_session
        self.alt_sessions = dict(alt_sessions or {})
        self._stop_event = asyncio.Event()

    async def cleanup_stale_files(self) -> None:
        """Remove site files for URLs no longer tracked, and commit deletions."""
        site_dir = self.repo.repo_path / "sites"
        if not site_dir.exists():
            return
        tracked_safe_names = {_safe_name(u["name"]) for u in self.state.urls}
        removed = False
        for f in site_dir.iterdir():
            if not f.is_file():
                continue
            stem = f.stem
            if stem not in tracked_safe_names:
                LOG.info("Removing stale site file: %s", f.name)
                f.unlink()
                removed = True
        if removed:
            if self.repo.commit("Cleanup: remove stale site files"):
                self.repo.push()

    async def run(self) -> None:
        LOG.info("Tracker started")
        await self.cleanup_stale_files()
        while not self._stop_event.is_set():
            await self._ensure_steam_fresh()
            await self._check_due()
            await asyncio.sleep(30)

    def _all_steam_sessions(self) -> List[SteamSession]:
        sessions = list(self.alt_sessions.values())
        if self.steam_session is not None:
            sessions.append(self.steam_session)
        return sessions

    async def _ensure_steam_fresh(self) -> None:
        for session in self._all_steam_sessions():
            if session.is_authenticated and not session.session_fresh():
                LOG.warning("Steam session for %s near expiry; refreshing", session.username)
                await session.refresh_async()

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

        content, error = await self._fetch(url, self._steam_session_for(url_entry))
        if error:
            LOG.warning("Fetch error for %s (%s): %s", name, url, error)
            self.state.record_check(url_id, old_hash, changed=False, error=error)
            return

        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if new_hash == old_hash:
            self.state.record_check(url_id, new_hash, changed=False)
            return

        old_content = self.repo.read_current_content(name)
        self.repo.write_content(name, content)
        message = build_commit_message(name, url, old_content, content)
        changed = self.repo.commit(message)
        if changed:
            self.repo.push()
            LOG.info("Change detected and committed for %s", name)

        self.state.record_check(url_id, new_hash, changed=changed)

    def _steam_session_for(self, url_entry: dict) -> Optional[SteamSession]:
        session_name = str(url_entry.get("session", "")).strip()
        if session_name:
            session = self.alt_sessions.get(session_name)
            if session is not None:
                return session
            LOG.warning("Unknown Steam session %r for %s; using default", session_name, url_entry.get("name"))
        return self.steam_session

    async def _fetch(self, url: str, steam_session: Optional[SteamSession] = None) -> tuple:
        if steam_session and SteamSession.is_steam_url(url):
            return await steam_session.fetch_text(url)
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
