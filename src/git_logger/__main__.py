import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path

from .app import Application
from .config import Settings
from .steam_session import SteamSession


class _ExcludeFileOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not bool(getattr(record, "file_only", False))


def configure_logging(level: str, log_file: Path, max_bytes: int, backup_count: int) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_ExcludeFileOnly())
    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        handlers=[console, file_handler],
        force=True,
    )


def setup_steam(settings: Settings) -> int:
    """Interactively authenticate with Steam and save the session."""
    configure_logging(settings.log_level, settings.log_file, settings.log_max_bytes, settings.log_backup_count)
    if not settings.steam_username or not settings.steam_password:
        logging.error("STEAM_USERNAME and STEAM_PASSWORD must be set in .env")
        return 1
    session = SteamSession(
        settings.steam_username,
        settings.steam_password,
        settings.steam_cookie_file,
        settings.steam_token_file,
    )
    if session.ensure_authenticated(interactive=True):
        logging.info("Steam session established and saved for %s", settings.steam_username)
        return 0
    logging.error("Steam authentication failed")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Git Logger")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--setup-steam", action="store_true", help="Interactively authenticate with Steam and save session")
    args = parser.parse_args()
    settings = Settings.load(args.env_file)
    if args.setup_steam:
        sys.exit(setup_steam(settings))
    configure_logging(settings.log_level, settings.log_file, settings.log_max_bytes, settings.log_backup_count)
    sys.exit(asyncio.run(Application(settings).run()))


if __name__ == "__main__":
    main()
