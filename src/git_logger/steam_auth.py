"""Steam web authentication, persistent refresh tokens, and account isolation."""

import base64
import json
import logging
import os
import pickle
import time
import uuid
from pathlib import Path
from typing import Callable, Dict

import requests
import rsa


LOG = logging.getLogger(__name__)
API_BASE = "https://api.steampowered.com/IAuthenticationService"
FINALIZE_URL = "https://login.steampowered.com/jwt/finalizelogin"
CHECK_DEVICE_URL = "https://login.steampowered.com/jwt/checkdevice"

GUARD_NONE = 1
GUARD_EMAIL_CODE = 2
GUARD_DEVICE_CODE = 3
GUARD_DEVICE_CONFIRMATION = 4
GUARD_EMAIL_CONFIRMATION = 5


class SteamAuthenticationError(RuntimeError):
    """Raised when a usable Steam store session cannot be established."""


class SteamAuthenticator:
    """Creates Store cookies from credentials or a saved web refresh token."""

    def __init__(
        self,
        session: requests.Session,
        account_name: str,
        password: str,
        cookie_file: Path,
        token_file: Path,
        timeout: float,
        verify: Callable[[], bool],
        input_fn: Callable[[str], str] = input,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self.account_name = account_name.strip()
        self.password = password
        self.cookie_file = Path(cookie_file)
        self.token_file = Path(token_file)
        self.timeout = timeout
        self.verify = verify
        self.input = input_fn
        self.sleep = sleep_fn

    def ensure_authenticated(self, force_login: bool = False, interactive: bool = True) -> bool:
        tokens = self._load_tokens()
        saved_account = str(tokens.get("account_name", "")).strip()
        account_matches = bool(saved_account) and saved_account.casefold() == self.account_name.casefold()

        if force_login or (saved_account and not account_matches):
            LOG.info("Steam account changed from %s to %s; ignoring old session", saved_account, self.account_name)
            self.session.cookies.clear()
            tokens = {}
            account_matches = False

        if not force_login and account_matches and self._load_cookies() and self.verify():
            LOG.info("Steam session restored for %s", self.account_name)
            return True

        refresh_token = str(tokens.get("refresh_token", "")) if account_matches else ""
        if not force_login and refresh_token and not self._jwt_expired(refresh_token):
            LOG.info("Refreshing Steam web session for %s", self.account_name)
            self.session.cookies.clear()
            try:
                steamid = self._steamid_from_token(refresh_token) or str(tokens.get("steamid", ""))
                self._create_web_cookies(refresh_token, steamid)
                if self.verify():
                    self._save_session(refresh_token, steamid)
                    return True
            except (SteamAuthenticationError, requests.RequestException, ValueError, OSError) as exc:
                LOG.warning("Steam refresh failed; full login required: %s", exc)

        if not interactive:
            LOG.error(
                "Steam login requires interaction. Run interactively locally, "
                "then deploy the generated data files."
            )
            return False
        if not self.account_name or not self.password:
            LOG.error("STEAM_USERNAME and STEAM_PASSWORD are required for a new Steam login")
            return False

        try:
            self.session.cookies.clear()
            refresh_token, steamid = self._full_login()
            self._save_tokens(refresh_token, steamid)
            self._create_web_cookies(refresh_token, steamid)
            if not self.verify():
                raise SteamAuthenticationError("Steam rejected the newly created Store session")
            self._save_session(refresh_token, steamid)
            LOG.info("New Steam session saved for %s", self.account_name)
            return True
        except (SteamAuthenticationError, requests.RequestException, ValueError, KeyError, OSError) as exc:
            LOG.error("Steam login failed: %s", exc)
            return False

    def refresh_authenticated_session(self) -> bool:
        """Force fresh Store cookies from the saved refresh token."""
        tokens = self._load_tokens()
        saved_account = str(tokens.get("account_name", "")).strip()
        if not saved_account or saved_account.casefold() != self.account_name.casefold():
            LOG.error("Cannot refresh Steam session: saved token belongs to another account")
            return False
        refresh_token = str(tokens.get("refresh_token", ""))
        if not refresh_token or self._jwt_expired(refresh_token):
            LOG.error("Cannot refresh Steam session: refresh token is missing or expired")
            return False
        try:
            steamid = self._steamid_from_token(refresh_token) or str(tokens.get("steamid", ""))
            self.session.cookies.clear()
            self._create_web_cookies(refresh_token, steamid)
            if not self.verify():
                raise SteamAuthenticationError("Steam rejected the refreshed Store session")
            self._save_session(refresh_token, steamid)
            LOG.info("Steam Store session refreshed for %s", self.account_name)
            return True
        except (SteamAuthenticationError, requests.RequestException, ValueError, OSError) as exc:
            LOG.error("Steam session recovery failed: %s", exc)
            return False

    def _full_login(self) -> tuple:
        key_payload = self._request_json(
            "GET",
            "{}/GetPasswordRSAPublicKey/v1".format(API_BASE),
            params={"account_name": self.account_name},
        )
        public_key = rsa.PublicKey(
            int(str(key_payload["publickey_mod"]), 16),
            int(str(key_payload["publickey_exp"]), 16),
        )
        encrypted = base64.b64encode(rsa.encrypt(self.password.encode("utf-8"), public_key)).decode("ascii")
        begin = self._api_post(
            "BeginAuthSessionViaCredentials",
            {
                "account_name": self.account_name,
                "encrypted_password": encrypted,
                "encryption_timestamp": str(key_payload["timestamp"]),
                "remember_login": True,
                "persistence": 1,
                "website_id": "Community",
                "device_details": {
                    "device_friendly_name": self.session.headers.get("User-Agent", "Git-Logger"),
                    "platform_type": 2,
                },
            },
        )
        client_id = str(begin.get("client_id", ""))
        request_id = str(begin.get("request_id", ""))
        steamid = str(begin.get("steamid", ""))
        if not client_id or not request_id or not steamid:
            raise SteamAuthenticationError("Steam rejected username or password")

        confirmations = begin.get("allowed_confirmations") or []
        types = {int(item.get("confirmation_type", 0)) for item in confirmations}
        if GUARD_DEVICE_CONFIRMATION in types:
            print("\n[STEAM] Bitte den Login jetzt in der Steam-App bestaetigen.", flush=True)
        elif GUARD_EMAIL_CONFIRMATION in types:
            print("\n[STEAM] Bitte den Login-Link in der Steam-E-Mail bestaetigen.", flush=True)
        elif GUARD_EMAIL_CODE in types:
            self._request_email(client_id, steamid)
            detail = next(
                (str(item.get("associated_message", "")) for item in confirmations
                 if int(item.get("confirmation_type", 0)) == GUARD_EMAIL_CODE),
                "",
            )
            code = self._env_guard_code()
            if not code:
                prompt = "\n[STEAM] Steam-Guard-Code aus der E-Mail{}: ".format(
                    " ({})".format(detail) if detail else ""
                )
                code = self.input(prompt).strip()
            if not code:
                raise SteamAuthenticationError("No Steam Guard email code entered")
            self._submit_guard_code(client_id, steamid, code, GUARD_EMAIL_CODE)
        elif GUARD_DEVICE_CODE in types:
            code = self._env_guard_code()
            if not code:
                code = self.input("\n[STEAM] Steam-Guard-Code aus der App: ").strip()
            if not code:
                raise SteamAuthenticationError("No Steam Guard app code entered")
            self._submit_guard_code(client_id, steamid, code, GUARD_DEVICE_CODE)
        elif types and GUARD_NONE not in types:
            raise SteamAuthenticationError("Unsupported Steam Guard methods: {}".format(sorted(types)))

        poll_interval = max(float(begin.get("interval") or 1.0), 0.5)
        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            poll = self._api_post(
                "PollAuthSessionStatus",
                {"client_id": client_id, "request_id": request_id},
            )
            refresh_token = str(poll.get("refresh_token", ""))
            if refresh_token:
                actual_account = str(poll.get("account_name", ""))
                if actual_account and actual_account.casefold() != self.account_name.casefold():
                    raise SteamAuthenticationError("Steam authenticated a different account")
                return refresh_token, self._steamid_from_token(refresh_token) or steamid
            new_client_id = poll.get("new_client_id")
            if new_client_id:
                client_id = str(new_client_id)
            self.sleep(poll_interval)
        raise SteamAuthenticationError("Steam Guard confirmation timed out after 180 seconds")

    def _request_email(self, client_id: str, steamid: str) -> None:
        response = self.session.post(
            CHECK_DEVICE_URL,
            files=self._multipart({"clientid": client_id, "steamid": steamid}),
            headers={"Origin": "https://steamcommunity.com", "Referer": "https://steamcommunity.com/"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise SteamAuthenticationError("Steam could not send the email code (HTTP {})".format(response.status_code))

    def _submit_guard_code(self, client_id: str, steamid: str, code: str, code_type: int) -> None:
        self._api_post(
            "UpdateAuthSessionWithSteamGuardCode",
            {"client_id": client_id, "steamid": steamid, "code": code, "code_type": code_type},
        )

    def _create_web_cookies(self, refresh_token: str, steamid: str) -> None:
        if not steamid:
            raise SteamAuthenticationError("Steam refresh token has no SteamID")
        session_id = uuid.uuid4().hex[:24]
        for domain in ("login.steampowered.com", "steamcommunity.com", "store.steampowered.com"):
            self.session.cookies.set("sessionid", session_id, domain=domain, path="/")
        response = self.session.post(
            FINALIZE_URL,
            files=self._multipart({
                "nonce": refresh_token,
                "sessionid": session_id,
                "redir": "https://steamcommunity.com/login/home/?goto=",
            }),
            headers={"Origin": "https://steamcommunity.com", "Referer": "https://steamcommunity.com/"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise SteamAuthenticationError("Steam finalizelogin returned HTTP {}".format(response.status_code))
        payload = response.json()
        if payload.get("error"):
            raise SteamAuthenticationError("Steam finalizelogin error {}".format(payload["error"]))
        transfers = payload.get("transfer_info")
        if not isinstance(transfers, list):
            raise SteamAuthenticationError("Steam finalizelogin returned no cookie transfers")
        transfer_steamid = str(payload.get("steamID") or steamid)
        for transfer in transfers:
            url = str(transfer.get("url", ""))
            params = dict(transfer.get("params") or {})
            if not url.startswith("https://"):
                continue
            params["steamID"] = transfer_steamid
            transfer_response = self.session.post(
                url,
                files=self._multipart(params),
                timeout=self.timeout,
            )
            if transfer_response.status_code >= 400:
                raise SteamAuthenticationError(
                    "Steam cookie transfer returned HTTP {}".format(transfer_response.status_code)
                )
            try:
                transfer_payload = transfer_response.json()
            except ValueError:
                transfer_payload = {}
            if transfer_payload.get("result") not in (None, 1):
                raise SteamAuthenticationError(
                    "Steam cookie transfer failed with EResult {}".format(transfer_payload["result"])
                )
        store_cookies = [
            cookie for cookie in self.session.cookies
            if cookie.name == "steamLoginSecure" and "store.steampowered.com" in (cookie.domain or "")
        ]
        if not store_cookies:
            domains = sorted({cookie.domain for cookie in self.session.cookies if cookie.name == "steamLoginSecure"})
            raise SteamAuthenticationError(
                "Steam transfers did not create the Store cookie (received domains: {})".format(domains)
            )

    def _api_post(self, method: str, payload: Dict[str, object]) -> Dict[str, object]:
        return self._request_json(
            "POST",
            "{}/{}/v1".format(API_BASE, method),
            data={"input_json": json.dumps(payload, separators=(",", ":"))},
        )

    def _request_json(self, method: str, url: str, **kwargs) -> Dict[str, object]:
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            raise SteamAuthenticationError("Steam auth API returned HTTP {}".format(response.status_code))
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamAuthenticationError("Steam auth API returned invalid JSON") from exc
        if payload.get("eresult") not in (None, 1):
            raise SteamAuthenticationError("Steam auth failed with EResult {}".format(payload["eresult"]))
        result = payload.get("response", payload)
        if not isinstance(result, dict):
            raise SteamAuthenticationError("Steam auth API returned an invalid response")
        return result

    def _load_cookies(self) -> bool:
        if not self.cookie_file.exists():
            return False
        try:
            with self.cookie_file.open("rb") as handle:
                cookies = pickle.load(handle)
            self.session.cookies.clear()
            self.session.cookies.update(cookies)
            return True
        except Exception as exc:
            LOG.warning("Could not load Steam cookies: %s", exc)
            return False

    def _load_tokens(self) -> Dict[str, object]:
        if not self.token_file.exists():
            return {}
        try:
            payload = json.loads(self.token_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError) as exc:
            LOG.warning("Could not load Steam tokens: %s", exc)
            return {}

    def _save_session(self, refresh_token: str, steamid: str) -> None:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_tmp = self.cookie_file.with_suffix(self.cookie_file.suffix + ".tmp")
        with cookie_tmp.open("wb") as handle:
            pickle.dump(self.session.cookies, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(str(cookie_tmp), str(self.cookie_file))
        self._save_tokens(refresh_token, steamid)
        try:
            self.cookie_file.chmod(0o600)
        except OSError:
            pass

    def _save_tokens(self, refresh_token: str, steamid: str) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        token_tmp = self.token_file.with_suffix(self.token_file.suffix + ".tmp")
        token_tmp.write_text(
            json.dumps(
                {"account_name": self.account_name, "steamid": steamid, "refresh_token": refresh_token},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(str(token_tmp), str(self.token_file))
        try:
            self.token_file.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _multipart(values: Dict[str, object]) -> Dict[str, tuple]:
        return {key: (None, str(value)) for key, value in values.items()}

    @staticmethod
    def _decode_jwt(token: str) -> Dict[str, object]:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            return json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    @classmethod
    def _jwt_expired(cls, token: str) -> bool:
        try:
            return int(cls._decode_jwt(token).get("exp", 0)) <= int(time.time()) + 30
        except (TypeError, ValueError):
            return True

    @classmethod
    def _steamid_from_token(cls, token: str) -> str:
        return str(cls._decode_jwt(token).get("sub", ""))

    @staticmethod
    def _env_guard_code() -> str:
        """Read Steam Guard code from STEAM_GUARD_CODE env var if set."""
        code = os.getenv("STEAM_GUARD_CODE", "").strip()
        if code:
            LOG.info("Using Steam Guard code from STEAM_GUARD_CODE environment variable")
        return code
