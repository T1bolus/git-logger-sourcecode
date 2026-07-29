"""Async wrapper around Steam authentication for fetching community pages."""

import asyncio
import html
import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

from .steam_auth import SteamAuthenticator, SteamAuthenticationError

LOG = logging.getLogger(__name__)


def _noop_input(_prompt: str) -> str:
    raise EOFError("No interactive input available")


class SteamSession:
    """Manages an authenticated Steam requests.Session and provides async fetch."""

    def __init__(
        self,
        username: str,
        password: str,
        cookie_file: Path,
        token_file: Path,
        timeout: float = 10.0,
        input_fn: Callable[[str], str] = _noop_input,
    ) -> None:
        self.username = username
        self.password = password
        self.cookie_file = cookie_file
        self.token_file = token_file
        self.timeout = timeout
        self.input_fn = input_fn
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        })
        self._authenticator: Optional[SteamAuthenticator] = None
        self._auth_error: str = ""
        self._authenticated: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def auth_error(self) -> str:
        return self._auth_error

    def ensure_authenticated(self, interactive: bool = True) -> bool:
        auth = SteamAuthenticator(
            self.session,
            self.username,
            self.password,
            self.cookie_file,
            self.token_file,
            self.timeout,
            self._verify,
            input_fn=self.input_fn,
        )
        self._authenticator = auth
        try:
            result = auth.ensure_authenticated(interactive=interactive)
            self._authenticated = result
            if not result:
                self._auth_error = "Steam authentication returned False"
            return result
        except (SteamAuthenticationError, EOFError, Exception) as exc:
            self._auth_error = str(exc)
            LOG.warning("Steam auth failed: %s", exc)
            return False

    def _verify(self) -> bool:
        try:
            response = self.session.get(
                "https://steamcommunity.com/",
                allow_redirects=True,
                timeout=self.timeout,
            )
            return response.status_code == 200 and "steamLoginSecure" in str(self.session.cookies)
        except requests.RequestException as exc:
            LOG.error("Steam verify failed: %s", exc)
            return False

    def fetch_text_sync(self, url: str) -> tuple:
        if not self._authenticated:
            return "", "Steam not authenticated"
        try:
            resolved_url = self._resolve_steam_url(url)
            if resolved_url != url:
                LOG.info("Resolved Steam URL: %s -> %s", url, resolved_url)
            response = self.session.get(resolved_url, allow_redirects=True, timeout=self.timeout)
            if response.status_code != 200:
                return "", "HTTP {}".format(response.status_code)
            text = response.text
            if self._is_games_page(resolved_url):
                games = self._extract_game_list(text)
                if games:
                    return json.dumps(games, indent=2, ensure_ascii=False), ""
            return text, ""
        except requests.Timeout:
            return "", "Timeout"
        except requests.RequestException as exc:
            return "", str(exc)
        except Exception as exc:
            return "", str(exc)

    def _is_games_page(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.hostname not in ("steamcommunity.com", "www.steamcommunity.com"):
            return False
        return bool(re.search(r'/games', parsed.path))

    @staticmethod
    def _extract_game_list(page_html: str) -> list | None:
        m = re.search(r'SSR\.renderContext\s*=\s*JSON\.parse\("(.+)"\)\s*;', page_html)
        if not m:
            LOG.warning("SSR.renderContext not found in page")
            return None
        try:
            inner = json.loads('"' + m.group(1) + '"')
            data = json.loads(inner)
            qd = json.loads(data.get('queryData', '{}'))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            LOG.warning("Failed to parse SSR renderContext: %s", exc)
            return None
        for q in qd.get('queries', []):
            qkey = q.get('queryKey', [])
            if len(qkey) >= 1 and qkey[0] == 'OwnedGames':
                raw_games = q.get('state', {}).get('data')
                if not isinstance(raw_games, list):
                    return None
                games = [{"appid": g["appid"], "name": g["name"]} for g in raw_games if "appid" in g and "name" in g]
                games.sort(key=lambda x: x["appid"])
                return games if games else None
        LOG.warning("OwnedGames query not found in SSR data")
        return None

    def _resolve_steam_url(self, url: str) -> str:
        """Resolve vanity /id/{name}/ URLs to /profiles/{steamid}/ for games pages."""
        parsed = urlparse(url)
        if parsed.hostname not in ("steamcommunity.com", "www.steamcommunity.com"):
            return url
        if not re.search(r'^/id/[^/]+/games', parsed.path):
            return url
        vanity = parsed.path.split('/')[2]
        try:
            resp = self.session.get(
                "https://steamcommunity.com/id/{}/?xml=1".format(vanity),
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return url
            match = re.search(r'<steamID64>(\d+)</steamID64>', resp.text)
            if not match:
                return url
            steamid = match.group(1)
            new_path = re.sub(r'^/id/[^/]+', '/profiles/{}'.format(steamid), parsed.path)
            new_url = "{}://{}{}".format(parsed.scheme or "https", parsed.hostname, new_path)
            if parsed.query:
                new_url += "?" + parsed.query
            if "l=" not in new_url:
                new_url += ("&" if parsed.query else "?") + "l=english"
            return new_url
        except Exception:
            return url

    async def fetch_text(self, url: str) -> tuple:
        return await asyncio.to_thread(self.fetch_text_sync, url)

    @staticmethod
    def is_steam_url(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(d in host for d in ["steamcommunity.com", "steampowered.com"])
