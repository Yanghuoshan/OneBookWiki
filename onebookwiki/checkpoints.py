"""Atomic, file-backed checkpoints for resumable book generation."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class CheckpointStore:
    def __init__(self, root: Path, run_id: str | None = None):
        self.root = root
        self.directory = root / ".onebookwiki" / "checkpoints"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.path = self.directory / f"run-{self.run_id}.json"
        self.data = self._load() or {"run_id": self.run_id, "status": "pending", "nodes": {}}
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_unlocked(self) -> None:
        """Persist current state to disk. Caller must hold ``self._lock``."""
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        latest = self.directory / "latest.json"
        latest_tmp = latest.with_suffix(".tmp")
        latest_tmp.write_text(json.dumps({"run_id": self.run_id}, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(latest_tmp, latest)

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def node(self, node_id: str) -> dict:
        with self._lock:
            return self.data.setdefault("nodes", {}).setdefault(node_id, {"node_id": node_id, "status": "pending", "attempts": 0})

    def reusable(self, node_id: str, input_hash: str, prompt_hash: str, model_hash: str, artifact_path: Path | None = None, artifact_hash: str | None = None) -> bool:
        node = self.data.get("nodes", {}).get(node_id, {})
        if node.get("status") != "completed" or node.get("input_hash") != input_hash or node.get("prompt_hash") != prompt_hash or node.get("model_hash") != model_hash:
            return False
        target = artifact_path or (self.root / node.get("artifact_path", ""))
        if not target.is_file():
            return False
        if artifact_hash:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            return actual == artifact_hash
        return True

    def mark(self, node_id: str, status: str, **values: object) -> dict:
        with self._lock:
            node = self.data.setdefault("nodes", {}).setdefault(node_id, {"node_id": node_id, "status": "pending", "attempts": 0})
            node.update(status=status, **values)
            if status == "running":
                node["attempts"] = int(node.get("attempts", 0)) + 1
            self.data["status"] = status if status in {"failed", "completed"} else "running"
            self._save_unlocked()
            return node

    def invalidate_downstream(self, changed: set[str]) -> None:
        nodes = self.data.get("nodes", {})
        for node in nodes.values():
            if set(node.get("dependencies", ())) & changed:
                node["status"] = "pending"
                changed.add(str(node.get("node_id")))
        self.save()

    @classmethod
    def latest(cls, root: Path) -> "CheckpointStore | None":
        pointer = root / ".onebookwiki" / "checkpoints" / "latest.json"
        if not pointer.is_file():
            return None
        try:
            run_id = json.loads(pointer.read_text(encoding="utf-8")).get("run_id")
        except (OSError, ValueError, TypeError):
            return None
        return cls(root, str(run_id)) if run_id else None


@contextmanager
def project_lock(root: Path) -> Iterator[None]:
    """Best-effort cross-platform exclusive lock for one generation process."""
    lock_path = root / ".onebookwiki" / "generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (ImportError, OSError):
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ImportError, OSError):
                pass
        yield
    finally:
        try:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        handle.close()
