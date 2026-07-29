import json
import logging
import os
import time
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

LOG = logging.getLogger(__name__)


def _generate_id(state: Dict[str, Any]) -> int:
    used = {u["id"] for u in state["urls"]}
    next_id = 1
    while next_id in used:
        next_id += 1
    return next_id


class StateStore:
    def __init__(self, state_file: Path, event_limit: int = 500) -> None:
        self.state_file = state_file
        self._sequence = 0
        self._events: Deque[Dict[str, Any]] = deque(maxlen=event_limit)
        self._state: Dict[str, Any] = {"urls": []}
        self._load()

    def _load(self) -> None:
        path = self.state_file
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("urls"), list):
                self._state = payload
                LOG.info("Restored %d tracked URLs from %s", len(self._state["urls"]), path)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Cannot restore state from %s: %s", path, exc)

    def _persist(self) -> None:
        path = self.state_file
        temporary = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError) as exc:
            LOG.error("Cannot persist state to %s: %s", path, exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def emit(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        self._sequence += 1
        event = {"sequence": self._sequence, "time": time.time(), "type": event_type, **payload}
        self._events.append(event)
        return event

    @property
    def urls(self) -> List[Dict[str, Any]]:
        return self._state["urls"]

    def get_url(self, url_id: int) -> Optional[Dict[str, Any]]:
        for u in self._state["urls"]:
            if u["id"] == url_id:
                return u
        return None

    def add_url(self, url: str, name: str = "", interval: int = 0) -> Dict[str, Any]:
        entry = {
            "id": _generate_id(self._state),
            "url": url,
            "name": name or url,
            "interval": interval or 3600,
            "enabled": True,
            "last_hash": "",
            "last_checked": 0.0,
            "last_changed": 0.0,
            "error": "",
        }
        self._state["urls"].append(entry)
        self._persist()
        self.emit("url_added", url=url, name=entry["name"], url_id=entry["id"])
        return entry

    def remove_url(self, url_id: int) -> bool:
        before = len(self._state["urls"])
        self._state["urls"] = [u for u in self._state["urls"] if u["id"] != url_id]
        if len(self._state["urls"]) < before:
            self._persist()
            self.emit("url_removed", url_id=url_id)
            return True
        return False

    def update_url(self, url_id: int, **updates: Any) -> bool:
        for u in self._state["urls"]:
            if u["id"] == url_id:
                allowed = {"url", "name", "interval", "enabled"}
                for key, value in updates.items():
                    if key in allowed:
                        u[key] = value
                self._persist()
                self.emit("url_updated", url_id=url_id, **updates)
                return True
        return False

    def record_check(self, url_id: int, content_hash: str, changed: bool, error: str = "") -> None:
        for u in self._state["urls"]:
            if u["id"] == url_id:
                u["last_checked"] = time.time()
                u["error"] = error
                if changed and not error:
                    u["last_hash"] = content_hash
                    u["last_changed"] = time.time()
                self._persist()
                self.emit(
                    "url_checked",
                    url_id=url_id,
                    changed=changed,
                    error=error,
                    name=u["name"],
                    url=u["url"],
                )
                return

    def events_since(self, sequence: int) -> List[Dict[str, Any]]:
        return [e for e in self._events if e["sequence"] > sequence]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "sequence": self._sequence,
            "urls": deepcopy(self._state["urls"]),
            "events": list(self._events),
        }
