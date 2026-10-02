"""Small atomic, private file store shared by CLI and HTTP."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))


class DomainError(ValueError):
    pass


class ConflictError(DomainError):
    pass


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def parse_time(value: str | datetime) -> datetime:
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise DomainError("时间必须为含时区的 ISO 8601 格式") from exc
    if dt.tzinfo is None:
        raise DomainError("时间缺少时区，请明确北京时间或 UTC")
    return dt.astimezone(CN_TZ)


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value) or ".." in value:
        raise DomainError("记录标识不合法")
    return value


def stable_id(prefix: str, request_id: str) -> str:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 250:
        raise DomainError("缺少有效 request_id；重试时请使用原标识")
    return safe_id(prefix + "-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24])


class Store:
    _locks: dict[str, threading.RLock] = {}
    _local = threading.local()

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("ASHARE_DATA_DIR") or PROJECT_ROOT / "data").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._key = str(self.root)
        self._lock = self._locks.setdefault(self._key, threading.RLock())

    def path(self, rel: str | Path) -> Path:
        p = (self.root / rel).resolve()
        if p == self.root or not p.is_relative_to(self.root):
            raise DomainError("禁止访问数据目录之外的路径")
        return p

    def read(self, rel: str | Path, default=None):
        p = self.path(rel)
        if not p.exists():
            return copy.deepcopy(default)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise DomainError(f"数据文件损坏，请保留原件核查：{rel}") from exc

    def list(self, rel: str | Path) -> list[dict]:
        p = self.path(rel)
        if not p.exists():
            return []
        return [self.read(f.relative_to(self.root)) for f in sorted(p.glob("*.json"))]

    @contextmanager
    def transaction(self):
        with self._lock:
            held = getattr(self._local, "held", {})
            if held.get(self._key, 0):
                held[self._key] += 1
                try:
                    yield
                finally:
                    held[self._key] -= 1
                return
            self._local.held = held
            lock_file = self.root / ".write.lock"
            with lock_file.open("a+b") as fh:
                if fh.tell() == 0:
                    fh.write(b"0")
                    fh.flush()
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fh.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise ConflictError("另一项写入仍在进行，请稍后重试同一请求")
                        time.sleep(0.03)
                held[self._key] = 1
                try:
                    yield
                finally:
                    held.pop(self._key, None)
                    fh.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh, fcntl.LOCK_UN)

    def write(self, rel: str | Path, value, overwrite: bool = False):
        with self.transaction():
            p = self.path(rel)
            if p.exists() and not overwrite:
                if self.read(rel) == value:
                    return value
                raise ConflictError(f"记录已存在且内容不同，禁止覆盖：{rel}")
            try:
                text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            except (TypeError, ValueError) as exc:
                raise DomainError("记录包含不可保存的数据或非有限数字") from exc
            self.write_text(rel, text)
        return value

    def write_text(self, rel: str | Path, text: str):
        with self.transaction():
            p = self.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            temp = p.with_name(p.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temp.open("w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, p)
            finally:
                if temp.exists():
                    temp.unlink()

