import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError("{} must be >= {}".format(name, minimum))
    return value


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError("{} must be >= {}".format(name, minimum))
    return value


@dataclass(frozen=True)
class AltSteamSession:
    """A secondary Steam session, referenced by name from URL entries."""

    name: str
    username: str
    cookie_file: Path
    token_file: Path
    password: str = ""


def _parse_alt_sessions(raw: str, data_dir: Path) -> Tuple[List[AltSteamSession], str]:
    raw = (raw or "").strip()
    if not raw:
        return [], ""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return [], "STEAM_ALT_SESSIONS is not valid JSON: {}".format(exc)
    if not isinstance(payload, list):
        return [], "STEAM_ALT_SESSIONS must be a JSON list of session objects"
    sessions: List[AltSteamSession] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            return [], "STEAM_ALT_SESSIONS entry #{} must be an object".format(index)
        name = str(item.get("name", "")).strip()
        username = str(item.get("username", "")).strip()
        if not name or not username:
            return [], "STEAM_ALT_SESSIONS entry #{} requires 'name' and 'username'".format(index)
        sessions.append(
            AltSteamSession(
                name=name,
                username=username,
                password=str(item.get("password", "")),
                cookie_file=Path(str(item.get("cookie_file", str(data_dir / "steamcookies.pkl")))),
                token_file=Path(str(item.get("token_file", str(data_dir / "steam_tokens.json")))),
            )
        )
    return sessions, ""


@dataclass(frozen=True)
class Settings:
    log_level: str = "INFO"
    log_file: Path = Path("logs/git-logger.log")
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8814

    default_interval: int = 3600

    git_user: str = "Git Logger"
    git_email: str = "logger@example.com"

    github_token: str = ""
    github_repo: str = ""

    steam_username: str = ""
    steam_password: str = ""
    steam_cookie_file: Path = Path("data/steamcookies.pkl")
    steam_token_file: Path = Path("data/steam_tokens.json")

    alt_steam_sessions: Tuple[AltSteamSession, ...] = ()

    alt_steam_sessions_error: str = ""

    @classmethod
    def load(cls, env_file: str = ".env") -> "Settings":
        load_dotenv(env_file, override=False)
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        settings = cls(
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=Path(os.getenv("LOG_FILE", "logs/git-logger.log")),
            log_max_bytes=_int("LOG_MAX_BYTES", 10 * 1024 * 1024, 1024),
            log_backup_count=_int("LOG_BACKUP_COUNT", 5, 1),
            data_dir=data_dir,
            host=os.getenv("HOST", "0.0.0.0"),
            port=_int("PORT", 8814, 1),
            default_interval=_int("DEFAULT_INTERVAL", 3600, 60),
            git_user=os.getenv("GIT_USER", "Git Logger"),
            git_email=os.getenv("GIT_EMAIL", "logger@example.com"),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            github_repo=os.getenv("GITHUB_REPO", ""),
            steam_username=os.getenv("STEAM_USERNAME", ""),
            steam_password=os.getenv("STEAM_PASSWORD", ""),
            steam_cookie_file=Path(os.getenv("STEAM_COOKIE_FILE", str(data_dir / "steamcookies.pkl"))),
            steam_token_file=Path(os.getenv("STEAM_TOKEN_FILE", str(data_dir / "steam_tokens.json"))),
        )
        raw_alt = os.getenv("STEAM_ALT_SESSIONS", "")
        alt_sessions, alt_error = _parse_alt_sessions(raw_alt, data_dir)
        if not alt_error:
            return replace(settings, alt_steam_sessions=tuple(alt_sessions))
        return replace(settings, alt_steam_sessions_error=alt_error)

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.alt_steam_sessions_error:
            errors.append(self.alt_steam_sessions_error)
        if self.alt_steam_sessions:
            names = [s.name for s in self.alt_steam_sessions]
            if len(set(names)) != len(names):
                errors.append("STEAM_ALT_SESSIONS contains duplicate session names")
        if not self.git_user:
            errors.append("GIT_USER is required")
        if not self.git_email:
            errors.append("GIT_EMAIL is required")
        if self.github_repo and not self.github_token:
            errors.append("GITHUB_TOKEN is required when GITHUB_REPO is set")
        if self.github_token and not self.github_repo:
            errors.append("GITHUB_REPO is required when GITHUB_TOKEN is set")
        return errors
