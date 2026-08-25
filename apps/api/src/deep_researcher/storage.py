import hashlib
import os
from pathlib import Path
from typing import BinaryIO
from uuid import UUID


class FileTooLargeError(ValueError):
    pass


class LocalObjectStore:
    """只允许 UUID 派生 key 的本地对象存储 Adapter。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put_attachment(
        self,
        workspace_id: UUID,
        attachment_id: UUID,
        source: BinaryIO,
        *,
        max_bytes: int,
    ) -> tuple[str, int, str]:
        key = f"workspaces/{workspace_id}/attachments/{attachment_id}/source"
        target = self.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError(f"文件不能超过 {max_bytes} 字节")
                    digest.update(chunk)
                    output.write(chunk)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return key, size, digest.hexdigest()

    def path_for(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError("非法对象路径")
        return candidate

    def read_bytes(self, key: str) -> bytes:
        """Read an object through the storage adapter without exposing its host path."""
        return self.path_for(key).read_bytes()
