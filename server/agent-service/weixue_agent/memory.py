"""The memory tool (`memory_20250818`) on /data/memory, one write at a time across the process."""
import asyncio
import os
from pathlib import Path
from typing import Any, Optional

from anthropic.lib.tools import ToolError
from anthropic.lib.tools._beta_builtin_memory_tool import BetaAsyncLocalFilesystemMemoryTool

from . import jsonlog
from .config import MEMORY_FILE_MAX_BYTES

WRITE_COMMANDS = ("create", "str_replace", "insert", "delete", "rename")
_WRITE_LOCK: Optional[asyncio.Lock] = None


def _write_lock() -> asyncio.Lock:
    global _WRITE_LOCK
    if _WRITE_LOCK is None:
        _WRITE_LOCK = asyncio.Lock()
    return _WRITE_LOCK


def memory_index(base_path: Path, max_files: int = 12, max_chars: int = 600) -> str:
    """`path: first line` of each memory file, newest first, for the per-turn context note."""
    root = Path(base_path) / "memories"
    if not root.is_dir():
        return ""
    files = sorted((p for p in root.rglob("*") if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    entries = []
    for path in files:
        try:
            first = path.read_text(errors="replace").strip().splitlines()[0] if path.stat().st_size else ""
        except (OSError, IndexError):
            first = ""
        entries.append(f"/memories/{path.relative_to(root)}: {first[:120]}")
    return jsonlog.truncate("; ".join(entries), max_chars) or ""


class MemoryTool(BetaAsyncLocalFilesystemMemoryTool):
    """Files live under `<base>/memories`; paths outside it or through a symlink are refused by the helper."""

    def __init__(self, base_path: Path, chat_id: Optional[str] = None,
                 max_file_bytes: int = MEMORY_FILE_MAX_BYTES):
        base = Path(base_path)
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        super().__init__(base_path=str(base))
        self.chat_id = chat_id
        self.max_file_bytes = max_file_bytes

    async def execute(self, command: Any) -> Any:
        jsonlog.log("memory command", chat_id=self.chat_id, event="memory", tool=command.command,
                    detail=jsonlog.truncate(getattr(command, "path", None) or getattr(command, "old_path", ""), 120))
        if command.command not in WRITE_COMMANDS:
            return await super().execute(command)
        async with _write_lock():
            await self._check_size(command)
            return await super().execute(command)

    async def _check_size(self, command: Any) -> None:
        if command.command == "create":
            added, path = len(command.file_text.encode("utf-8")), command.path
        elif command.command == "str_replace":
            added, path = len(command.new_str.encode("utf-8")), command.path
        elif command.command == "insert":
            added, path = len(command.insert_text.encode("utf-8")), command.path
        else:
            return
        current = 0
        if command.command != "create":
            target = await self._validate_path(path)
            if await target.exists():
                current = (await target.stat()).st_size
        if current + added > self.max_file_bytes:
            raise ToolError(f"Memory files are limited to {self.max_file_bytes // 1024} KB; keep {path} shorter.")
