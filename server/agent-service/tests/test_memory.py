"""The memory tool: one write at a time, paths confined to <memory>/memories, file size cap."""
import asyncio
import os

import pytest
from anthropic.lib.tools import ToolError

from weixue_agent.memory import MemoryTool


async def test_concurrent_writers_from_four_chats_all_land(tmp_path):
    tools = [MemoryTool(tmp_path, chat_id=f"c{i}") for i in range(4)]
    results = await asyncio.gather(*(t.call({"command": "create", "path": f"/memories/fact{i}.md",
                                             "file_text": f"fact {i}"}) for i, t in enumerate(tools)))
    assert all(r.startswith("File created successfully") for r in results)
    await asyncio.gather(*(t.call({"command": "str_replace", "path": "/memories/fact0.md", "old_str": "fact 0",
                                   "new_str": "fact 0"}) for t in tools[:1]))
    assert sorted(os.listdir(tmp_path / "memories")) == [f"fact{i}.md" for i in range(4)]
    assert oct(os.stat(tmp_path).st_mode & 0o777) == "0o700"


async def test_traversal_and_symlink_escape_are_refused(tmp_path):
    tool = MemoryTool(tmp_path / "memory")
    await tool.call({"command": "create", "path": "/memories/ok.md", "file_text": "ok"})
    with pytest.raises(ToolError):
        await tool.call({"command": "view", "path": "/memories/../../etc"})
    with pytest.raises(ToolError):
        await tool.call({"command": "view", "path": "/etc/passwd"})
    os.symlink(tmp_path, tmp_path / "memory" / "memories" / "escape")
    with pytest.raises(ToolError):
        await tool.call({"command": "create", "path": "/memories/escape/agent.db", "file_text": "x"})


async def test_file_size_is_capped(tmp_path):
    tool = MemoryTool(tmp_path, max_file_bytes=100)
    with pytest.raises(ToolError):
        await tool.call({"command": "create", "path": "/memories/big.md", "file_text": "x" * 101})
    await tool.call({"command": "create", "path": "/memories/small.md", "file_text": "x" * 60})
    with pytest.raises(ToolError):
        await tool.call({"command": "insert", "path": "/memories/small.md", "insert_line": 0, "insert_text": "y" * 60})


async def test_memory_index_reaches_the_context_note_of_quick_turns(make):
    h = await make()
    tool = MemoryTool(h.settings.memory_dir)
    await tool.call({"command": "create", "path": "/memories/bins.md", "file_text": "The bins go out on Thursday.\nGreen"})
    _, events = await h.send("When do the bins go out?")
    await h.wait_task(events[0]["task_id"], "done")
    note = h.api.requests[0]["messages"][-1]["content"][0]["text"]
    assert "memory: /memories/bins.md: The bins go out on Thursday." in note and "Green" not in note
