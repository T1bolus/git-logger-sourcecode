import asyncio
import logging
import signal
from typing import Optional

import aiohttp

from .config import Settings
from .repo_manager import RepoManager
from .state import StateStore
from .steam_session import SteamSession
from .tracker import Tracker
from .web import WebServer

LOG = logging.getLogger(__name__)


class Application:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = asyncio.Event()

        state_file = settings.data_dir / "state.json"
        self.state = StateStore(state_file)

        repo_path = settings.data_dir / "repo"
        self.repo = RepoManager(
            repo_path,
            settings.git_user,
            settings.git_email,
            settings.github_token,
            settings.github_repo,
        )

        self.session: Optional[aiohttp.ClientSession] = None
        self.steam_session: Optional[SteamSession] = None
        self.tracker: Optional[Tracker] = None
        self.web: Optional[WebServer] = None

    async def run(self) -> int:
        errors = self.settings.validate()
        if errors:
            for error in errors:
                LOG.error("Configuration: %s", error)
            return 2

        self.repo.ensure_repo()

        if self.settings.steam_username:
            self.steam_session = SteamSession(
                self.settings.steam_username,
                self.settings.steam_password,
                self.settings.steam_cookie_file,
                self.settings.steam_token_file,
            )
            LOG.info("Authenticating with Steam as %s", self.settings.steam_username)
            if not self.steam_session.ensure_authenticated(interactive=False):
                LOG.warning(
                    "Steam non-interactive login failed (no saved session). "
                    "Run 'python -m git_logger --setup-steam' for interactive login, "
                    "or use POST /api/steam/auth to authenticate via the API."
                )

        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)

        self.tracker = Tracker(
            self.state,
            self.repo,
            self.session,
            self.settings.default_interval,
            steam_session=self.steam_session,
        )
        self.web = WebServer(self.state, self.tracker, self.settings.host, self.settings.port)

        await self.web.start()
        self._install_signal_handlers()

        try:
            tracker_task = asyncio.create_task(self.tracker.run(), name="tracker")
            stop_wait = asyncio.create_task(self.stop_event.wait(), name="stop-wait")
            LOG.info("Git Logger is ready on http://%s:%s", self.settings.host, self.settings.port)

            done, pending = await asyncio.wait(
                {tracker_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return 0
        except Exception:
            LOG.exception("Application runtime failure")
            return 5
        finally:
            await self._shutdown()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.stop_event.set))

    async def _shutdown(self) -> None:
        LOG.info("Stopping Git Logger")
        if self.tracker:
            await self.tracker.stop()
        if self.web:
            await self.web.stop()
        if self.session:
            await self.session.close()
