import logging
import subprocess
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)


class RepoManager:
    def __init__(self, repo_path: Path, git_user: str, git_email: str, github_token: str = "", github_repo: str = "") -> None:
        self.repo_path = repo_path
        self.git_user = git_user
        self.git_email = git_email
        self.github_token = github_token
        self.github_repo = github_repo

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo_path)] + list(args),
            capture_output=True, text=True, check=check, timeout=30,
        )

    def ensure_repo(self) -> None:
        git_dir = self.repo_path / ".git"
        if not git_dir.exists():
            self.repo_path.mkdir(parents=True, exist_ok=True)
            self._git("init")
            self._git("config", "user.name", self.git_user)
            self._git("config", "user.email", self.git_email)
            LOG.info("Initialized git repo at %s", self.repo_path)
        else:
            self._git("config", "user.name", self.git_user)
            self._git("config", "user.email", self.git_email)

    def write_content(self, name: str, content: str) -> Path:
        site_dir = self.repo_path / "sites"
        site_dir.mkdir(parents=True, exist_ok=True)
        ext = ".json" if content.strip().startswith("[") or content.strip().startswith("{") else ".html"
        file_path = site_dir / "{}{}".format(_safe_name(name), ext)
        file_path.write_text(content, encoding="utf-8")
        return file_path

    def has_changes(self) -> bool:
        result = self._git("status", "--porcelain", check=False)
        return bool(result.stdout.strip())

    def commit(self, message: str) -> bool:
        if not self.has_changes():
            return False
        self._git("add", "-A")
        self._git("commit", "-m", message, "--allow-empty")
        LOG.info("Created commit: %s", message)
        return True

    def push(self) -> None:
        if not self.github_token or not self.github_repo:
            LOG.debug("GitHub push not configured, skipping")
            return
        remote_url = "https://{}@github.com/{}.git".format(self.github_token, self.github_repo)
        try:
            self._git("remote", "add", "origin", remote_url, check=False)
        except Exception:
            pass
        try:
            self._git("remote", "set-url", "origin", remote_url)
        except Exception:
            pass
        try:
            self._git("push", "origin", "HEAD:main", check=False)
            LOG.info("Pushed to GitHub: %s", self.github_repo)
        except subprocess.CalledProcessError as exc:
            if "fetch first" in exc.stderr or "non-fast-forward" in exc.stderr:
                LOG.warning("Push rejected (non-fast-forward), trying force push: %s", exc.stderr.strip())
                try:
                    self._git("push", "--force", "origin", "HEAD:main", check=False)
                    LOG.info("Force pushed to GitHub: %s", self.github_repo)
                except subprocess.CalledProcessError as exc2:
                    LOG.warning("Force push also failed: %s", exc2.stderr.strip())
            else:
                LOG.warning("Git push failed (will retry later): %s", exc.stderr.strip())

    def get_log(self, max_count: int = 20) -> str:
        try:
            result = self._git("log", "--oneline", "--max-count={}".format(max_count), check=False)
            return result.stdout
        except Exception:
            return ""

    def get_diff(self, name: str) -> str:
        safe_name = _safe_name(name)
        file_path = "sites/{}.html".format(safe_name)
        try:
            result = self._git("diff", "HEAD~1", "--", file_path, check=False)
            if result.stdout:
                return result.stdout
        except Exception:
            pass
        try:
            file_path = "sites/{}.json".format(safe_name)
            result = self._git("diff", "HEAD~1", "--", file_path, check=False)
            return result.stdout
        except Exception:
            return ""


def _safe_name(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return safe.strip().replace(" ", "_") or "untitled"
