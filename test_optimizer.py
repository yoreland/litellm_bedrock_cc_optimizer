"""
Unit tests for bedrock_optimizer.py

Run: python -m pytest test_optimizer.py -v
"""

import copy
import json
import pytest

from bedrock_optimizer import (
    modify_thinking,
    inject_cache_control,
    strip_unsupported_fields,
    _new_cache_marker,
    CACHE_TTL,
)


# ---------------------------------------------------------------------------
# Thinking Tests
# ---------------------------------------------------------------------------
class TestModifyThinking:
    def test_skip_haiku(self):
        data = {"messages": [{"role": "user", "content": "hi"}]}
        modified, action = modify_thinking(data, "bedrock/global.anthropic.claude-haiku-4-5")
        assert not modified
        assert "skip" in action.lower()

    def test_adaptive_opus_46(self):
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 8000},
        }
        modified, action = modify_thinking(data, "bedrock/global.anthropic.claude-opus-4-6-v1")
        assert modified
        assert data["thinking"]["type"] == "adaptive"
        assert data["output_config"]["effort"] == "max"
        assert "context-1m-2025-08-07" in data.get("anthropic_beta", [])

    def test_adaptive_already_set(self):
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "max"},
            "anthropic_beta": ["context-1m-2025-08-07"],
        }
        modified, action = modify_thinking(data, "bedrock/claude-sonnet-4-6")
        assert not modified
        assert "already" in action

    def test_legacy_sonnet_45_inject(self):
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8192,
        }
        modified, action = modify_thinking(data, "bedrock/anthropic.claude-sonnet-4-5")
        assert modified
        assert data["thinking"]["type"] == "enabled"
        assert data["thinking"]["budget_tokens"] == 8191

    def test_legacy_upgrade_budget(self):
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16000,
            "thinking": {"type": "enabled", "budget_tokens": 4096},
        }
        modified, action = modify_thinking(data, "bedrock/anthropic.claude-sonnet-4-5")
        assert modified
        assert data["thinking"]["budget_tokens"] == 15999
        assert "upgraded" in action

    def test_no_messages_passthrough(self):
        data = {"model": "test"}
        modified, action = modify_thinking(data, "bedrock/claude-opus-4-6")
        assert not modified
        assert "passthrough" in action


# ---------------------------------------------------------------------------
# Cache Control Tests
# ---------------------------------------------------------------------------
class TestInjectCacheControl:
    def test_inject_tools_and_system(self):
        data = {
            "tools": [
                {"name": "tool1", "description": "desc1"},
                {"name": "tool2", "description": "desc2"},
            ],
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "hello"},
            ],
        }
        added, action = inject_cache_control(data)
        assert added >= 2
        # Last tool should have cache_control
        assert "cache_control" in data["tools"][-1]
        # System should be converted to list with cache_control
        assert isinstance(data["system"], list)
        assert "cache_control" in data["system"][0]

    def test_inject_assistant_turn(self):
        data = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "how are you"},
            ],
        }
        added, action = inject_cache_control(data)
        assert added >= 1
        # Assistant content should be wrapped in list with cache_control
        assistant_msg = data["messages"][1]
        assert isinstance(assistant_msg["content"], list)
        assert "cache_control" in assistant_msg["content"][0]

    def test_ttl_upgrade_existing(self):
        data = {
            "tools": [
                {"name": "t1", "cache_control": {"type": "ephemeral"}},
                {"name": "t2", "cache_control": {"type": "ephemeral"}},
            ],
            "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}},
                    ],
                },
            ],
        }
        added, action = inject_cache_control(data)
        assert added == 0  # all 4 slots taken
        assert "ttl-upgrade" in action or "no-op" in action
        # Verify TTL was upgraded
        assert data["tools"][0]["cache_control"]["ttl"] == CACHE_TTL

    def test_skip_thinking_blocks(self):
        data = {
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "hmm..."},
                        {"type": "text", "text": "hello!"},
                    ],
                },
            ],
        }
        added, action = inject_cache_control(data)
        assert added >= 1
        # Should add cache_control to text block, not thinking
        assistant_content = data["messages"][1]["content"]
        for block in assistant_content:
            if block["type"] == "thinking":
                assert "cache_control" not in block
            elif block["type"] == "text":
                assert "cache_control" in block

    def test_max_4_breakpoints(self):
        data = {
            "tools": [{"name": f"t{i}"} for i in range(10)],
            "system": [{"type": "text", "text": "system prompt"}],
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "again"},
                {"role": "assistant", "content": "yo"},
            ],
        }
        added, action = inject_cache_control(data)
        # Should not exceed 4 total breakpoints
        total = 0
        for tool in data["tools"]:
            if "cache_control" in tool:
                total += 1
        sys = data["system"]
        if isinstance(sys, list):
            for s in sys:
                if isinstance(s, dict) and "cache_control" in s:
                    total += 1
        for msg in data["messages"]:
            content = msg.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and "cache_control" in c:
                        total += 1
            elif isinstance(content, str):
                pass  # already counted if wrapped
        assert total <= 4


# ---------------------------------------------------------------------------
# Strip Unsupported Fields
# ---------------------------------------------------------------------------
class TestStripUnsupported:
    def test_strip_defer_loading(self):
        data = {
            "tools": [
                {"name": "t1", "defer_loading": True},
                {"name": "t2"},
                {"name": "t3", "defer_loading": False},
            ],
        }
        modified = strip_unsupported_fields(data)
        assert modified
        for tool in data["tools"]:
            assert "defer_loading" not in tool

    def test_no_tools(self):
        data = {"messages": []}
        modified = strip_unsupported_fields(data)
        assert not modified


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
