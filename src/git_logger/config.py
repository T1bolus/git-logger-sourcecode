import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

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

    @classmethod
    def load(cls, env_file: str = ".env") -> "Settings":
        load_dotenv(env_file, override=False)
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        return cls(
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

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.git_user:
            errors.append("GIT_USER is required")
        if not self.git_email:
            errors.append("GIT_EMAIL is required")
        if self.github_repo and not self.github_token:
            errors.append("GITHUB_TOKEN is required when GITHUB_REPO is set")
        if self.github_token and not self.github_repo:
            errors.append("GITHUB_REPO is required when GITHUB_TOKEN is set")
        return errors
