"""Async wrapper around Steam authentication for fetching community pages."""

import asyncio
import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

import requests

from .steam_auth import SteamAuthenticator, SteamAuthenticationError

LOG = logging.getLogger(__name__)

_KNOWN_STEAMIDS: dict[str, str] = {
    "AlphaDuplo": "76561198061432306",
    "notsamix": "76561198077632097",
    "T1bolus": "76561198096096250",
}


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
        self._steamid_cache: dict[str, str] = {}

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
            return bool(
                response.status_code == 200
                and "steamLoginSecure" in str(self.session.cookies)
                and re.search(r'g_steamID\s*=\s*["\']?(\d{17})["\']?\s*;', response.text)
            )
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
            text, final_url, err = self._get(resolved_url)
            if err:
                return "", err
            is_games = self._is_games_page(resolved_url)
            if is_games and "/login/" in final_url:
                LOG.warning("Steam session expired while fetching %s; refreshing session", resolved_url)
                if self.refresh_session():
                    text, final_url, err = self._get(resolved_url)
                    if err:
                        return "", err
            if is_games:
                games = self._extract_game_list(text)
                if games:
                    return json.dumps(games, indent=2, ensure_ascii=False), ""
                if self._looks_logged_out(text):
                    return "", "Steam session not authenticated (games page returned login page)"
                return "", "Games page returned no SSR data for {}".format(resolved_url)
            return text, ""
        except requests.Timeout:
            return "", "Timeout"
        except requests.RequestException as exc:
            return "", str(exc)
        except Exception as exc:
            return "", str(exc)

    def _get(self, url: str) -> tuple:
        """GET a page, returning (text, final_url, error)."""
        response = self.session.get(url, allow_redirects=True, timeout=self.timeout)
        if response.status_code != 200:
            return "", response.url, "HTTP {}".format(response.status_code)
        return response.text, response.url, ""

    @staticmethod
    def _looks_logged_out(text: str) -> bool:
        return 'logged_in&quot;:false' in text

    def refresh_session(self) -> bool:
        """Force fresh web cookies from the saved refresh token."""
        if not self._authenticator:
            return False
        result = self._authenticator.refresh_authenticated_session()
        self._authenticated = result
        return result

    async def refresh_async(self) -> bool:
        return await asyncio.to_thread(self.refresh_session)

    def _steam_login_cookie_value(self) -> str:
        for cookie in self.session.cookies:
            if cookie.name == "steamLoginSecure" and "steamcommunity.com" in (cookie.domain or ""):
                return str(cookie.value)
        return ""

    def _cookie_expiry(self) -> Optional[float]:
        """Unix timestamp when the embedded JWT in the login cookie expires."""
        value = self._steam_login_cookie_value()
        if not value or "||" not in unquote(value):
            return None
        jwt = unquote(value).split("||", 1)[1]
        exp = SteamAuthenticator._decode_jwt(jwt).get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
        return None

    def session_fresh(self, margin: float = 3600.0) -> bool:
        """True if authenticated and the cookie JWT is not near expiry."""
        if not self._authenticated:
            return False
        exp = self._cookie_expiry()
        if exp is None:
            return False
        return exp > time.time() + margin

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

    @staticmethod
    def _force_english(url: str) -> str:
        """Strip any `l=` parameter and append `l=english`."""
        parsed = urlparse(url)
        if parsed.hostname not in ("steamcommunity.com", "www.steamcommunity.com"):
            return url
        query_parts = [p for p in parsed.query.split("&") if p and not p.startswith("l=")]
        new_q = "&".join(query_parts)
        if new_q:
            new_q += "&"
        new_q += "l=english"
        return "{}://{}{}?{}".format(parsed.scheme or "https", parsed.hostname, parsed.path, new_q)

    def _resolve_steam_url(self, url: str) -> str:
        """Resolve vanity /id/{name}/ URLs to /profiles/{steamid}/ for games pages."""
        parsed = urlparse(url)
        if parsed.hostname not in ("steamcommunity.com", "www.steamcommunity.com"):
            return url
        if not re.search(r'^/id/[^/]+/games', parsed.path):
            return self._force_english(url)
        vanity = parsed.path.split('/')[2]
        steamid = self._resolve_steamid(vanity)
        if not steamid:
            return self._force_english(url)
        new_path = re.sub(r'^/id/[^/]+', '/profiles/{}'.format(steamid), parsed.path)
        new_url = "{}://{}{}".format(parsed.scheme or "https", parsed.hostname, new_path)
        if parsed.query:
            new_url += "?" + parsed.query
        new_url = self._force_english(new_url)
        return new_url

    def _resolve_steamid(self, vanity: str) -> str | None:
        """Resolve a vanity name to a 64-bit SteamID."""
        cached = self._steamid_cache.get(vanity)
        if cached:
            return cached
        known = _KNOWN_STEAMIDS.get(vanity)
        if known:
            self._steamid_cache[vanity] = known
            return known
        steamid = self._resolve_steamid_xml(vanity)
        if steamid:
            self._steamid_cache[vanity] = steamid
            return steamid
        steamid = self._resolve_steamid_ssr(vanity)
        if steamid:
            self._steamid_cache[vanity] = steamid
        return steamid

    def _resolve_steamid_xml(self, vanity: str) -> str | None:
        """Resolve vanity name via XML endpoint with retry."""
        import time
        for attempt in range(5):
            try:
                resp = self.session.get(
                    "https://steamcommunity.com/id/{}/?xml=1".format(vanity),
                    timeout=self.timeout,
                )
                if resp.status_code == 429:
                    delay = (attempt + 1) * 3
                    LOG.warning("XML endpoint rate-limited (429), retrying in %ds", delay)
                    time.sleep(delay)
                    continue
                if resp.status_code != 200:
                    return None
                match = re.search(r'<steamID64>(\d+)</steamID64>', resp.text)
                if match:
                    return match.group(1)
                return None
            except Exception:
                if attempt == 4:
                    return None
                time.sleep(2)
        return None

    def _resolve_steamid_ssr(self, vanity: str) -> str | None:
        """Fallback: extract steamid from the games page SSR renderContext."""
        try:
            resp = self.session.get(
                "https://steamcommunity.com/id/{}/games/?l=english".format(vanity),
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            m = re.search(r'SSR\.renderContext\s*=\s*JSON\.parse\("([^"]+)"\)\s*;', resp.text)
            if not m:
                return None
            import json as _json
            inner = _json.loads('"' + m.group(1) + '"')
            data = _json.loads(inner)
            qd = _json.loads(data.get('queryData', '{}'))
            for q in qd.get('queries', []):
                qkey = q.get('queryKey', [])
                if len(qkey) >= 2 and qkey[0] == 'OwnedGames' and qkey[1].isdigit():
                    return qkey[1]
            return None
        except Exception:
            return None

    async def fetch_text(self, url: str) -> tuple:
        return await asyncio.to_thread(self.fetch_text_sync, url)

    @staticmethod
    def is_steam_url(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(d in host for d in ["steamcommunity.com", "steampowered.com"])
