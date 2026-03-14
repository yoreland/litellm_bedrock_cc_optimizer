"""
Unit tests for bedrock_optimizer.py (OpenAI format)

Run: python -m pytest test_optimizer.py -v
"""

import copy
import json
import pytest

from bedrock_optimizer import (
    inject_cache_control,
    _new_cache_marker,
    CACHE_TTL,
)


class TestInjectCacheControl:
    """Test cache injection on OpenAI-format messages."""

    def test_inject_system_message(self):
        """System message should get cache_control on last block."""
        data = {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "hello"},
            ],
        }
        added, action = inject_cache_control(data)
        assert added >= 1
        # System content should be converted to array with cache_control
        sys_msg = data["messages"][0]
        assert isinstance(sys_msg["content"], list)
        assert "cache_control" in sys_msg["content"][-1]

    def test_inject_assistant_turn(self):
        """Last assistant message should get cache_control."""
        data = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "how are you"},
            ],
        }
        added, action = inject_cache_control(data)
        assert added >= 1
        # Assistant content should be wrapped with cache_control
        assistant_msg = data["messages"][1]
        assert isinstance(assistant_msg["content"], list)
        assert "cache_control" in assistant_msg["content"][-1]

    def test_inject_system_and_assistant(self):
        """Both system and last assistant should get cache_control."""
        data = {
            "messages": [
                {"role": "system", "content": "You are an expert."},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "question"},
            ],
        }
        added, action = inject_cache_control(data)
        assert added == 2
        assert "system" in action
        assert "last_assistant" in action

    def test_ttl_value(self):
        """Cache markers should have correct TTL."""
        data = {
            "messages": [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "hello"},
            ],
        }
        inject_cache_control(data)
        sys_block = data["messages"][0]["content"][-1]
        assert sys_block["cache_control"]["ttl"] == CACHE_TTL

    def test_upgrade_existing_ttl(self):
        """Existing cache_control markers should have TTL upgraded."""
        data = {
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "question"},
                    ],
                },
            ],
        }
        added, action = inject_cache_control(data)
        # TTL should be upgraded on existing markers
        sys_cc = data["messages"][0]["content"][0]["cache_control"]
        assert sys_cc.get("ttl") == CACHE_TTL

    def test_skip_thinking_blocks(self):
        """Thinking blocks in assistant messages should be skipped."""
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
                {"role": "user", "content": "question"},
            ],
        }
        added, action = inject_cache_control(data)
        assert added >= 1
        assistant_content = data["messages"][1]["content"]
        for block in assistant_content:
            if block["type"] == "thinking":
                assert "cache_control" not in block
            elif block["type"] == "text":
                assert "cache_control" in block

    def test_max_4_breakpoints(self):
        """Should not exceed 4 total breakpoints."""
        data = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "u3"},
                {"role": "assistant", "content": "a3"},
                {"role": "user", "content": "u4"},
                {"role": "assistant", "content": "a4"},
                {"role": "user", "content": "u5"},
            ],
        }
        added, action = inject_cache_control(data)
        # Count total cache_control markers
        total = 0
        for msg in data["messages"]:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        total += 1
        assert total <= 4

    def test_no_messages(self):
        """Should handle empty messages gracefully."""
        data = {"model": "test"}
        added, action = inject_cache_control(data)
        assert added == 0
        assert "no-op" in action

    def test_existing_at_max(self):
        """If 4 markers already exist, don't add more."""
        data = {
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "u1", "cache_control": {"type": "ephemeral"}}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "a1", "cache_control": {"type": "ephemeral"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "u2", "cache_control": {"type": "ephemeral"}}],
                },
            ],
        }
        added, action = inject_cache_control(data)
        assert added == 0

    def test_2nd_assistant_injection(self):
        """Second-to-last assistant should also get a marker."""
        data = {
            "messages": [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "u3"},
            ],
        }
        added, action = inject_cache_control(data)
        # Should inject on both assistant messages
        assert added >= 2
        a1 = data["messages"][1]
        a2 = data["messages"][3]
        assert isinstance(a1["content"], list) and "cache_control" in a1["content"][-1]
        assert isinstance(a2["content"], list) and "cache_control" in a2["content"][-1]

    def test_user_only_no_injection(self):
        """With only user messages, only system (if present) gets marker."""
        data = {
            "messages": [
                {"role": "user", "content": "hello"},
            ],
        }
        added, action = inject_cache_control(data)
        assert added == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
