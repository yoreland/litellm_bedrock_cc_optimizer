"""
Bedrock Optimizer — LiteLLM Custom Callback Hook

Ports the key optimizations from claudecode-bedrock-proxy (Go) into a
LiteLLM async_pre_call_hook. Works at the OpenAI-format level since
LiteLLM's transformation layer handles conversion to Bedrock format.

Key insight: LiteLLM's pre_call_hook receives OpenAI-format data.
We inject cache_control markers here, and LiteLLM's bedrock converse
transformation automatically converts them to Bedrock's cachePoint format.

Optimizations:
1. Prompt Cache TTL upgrade (5m → 1h)
2. Prompt Cache breakpoint auto-injection (up to 4)
3. Strip unsupported fields

Reference: https://github.com/KevinZhao/claudecode-bedrock-proxy
"""

import copy
import logging
import os
from typing import Any, Dict, List, Literal, Optional, Tuple

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import DualCache, UserAPIKeyAuth

logger = logging.getLogger("bedrock_optimizer")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[bedrock-opt] %(message)s"))
    logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Configuration (env overridable)
# ---------------------------------------------------------------------------
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "1") == "1"
CACHE_TTL = os.environ.get("CACHE_TTL", "1h")
MAX_CACHE_BREAKPOINTS = 4

# Stats
_stats = {
    "requests": 0,
    "cache_injected": 0,
}


def _new_cache_marker() -> Dict[str, Any]:
    """Create a cache_control marker dict."""
    marker: Dict[str, Any] = {"type": "ephemeral"}
    if CACHE_TTL != "5m":
        marker["ttl"] = CACHE_TTL
    return marker


# ---------------------------------------------------------------------------
# Cache Control Injection (OpenAI format)
# ---------------------------------------------------------------------------
# In OpenAI format, messages look like:
#   {"role": "system", "content": "..."}
#   {"role": "user", "content": "..."}
#   {"role": "user", "content": [{"type": "text", "text": "..."}]}
#   {"role": "assistant", "content": "..."}
#
# We inject cache_control at the content-block level.
# LiteLLM's Bedrock transformation picks up cache_control and converts
# it to cachePoint automatically.

def _ensure_content_blocks(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert string content to content block array if needed."""
    content = msg.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
        msg["content"] = blocks
        return blocks
    if isinstance(content, list):
        return content
    return []


def _count_existing_cache_markers(messages: List[Dict[str, Any]]) -> int:
    """Count existing cache_control markers across all messages."""
    count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    count += 1
        elif isinstance(content, str):
            pass  # strings don't have cache_control
    return count


def _upgrade_existing_ttl(messages: List[Dict[str, Any]]) -> int:
    """Upgrade TTL on existing cache_control markers."""
    if CACHE_TTL == "5m":
        return 0
    upgraded = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    cc = block["cache_control"]
                    if isinstance(cc, dict):
                        cc["ttl"] = CACHE_TTL
                        upgraded += 1
    return upgraded


def inject_cache_control(data: Dict[str, Any]) -> Tuple[int, str]:
    """Inject cache_control breakpoints into OpenAI-format messages."""
    messages = data.get("messages")
    if not isinstance(messages, list) or len(messages) == 0:
        return 0, "no-op(no messages)"

    existing = _count_existing_cache_markers(messages)
    upgraded = _upgrade_existing_ttl(messages)

    budget = MAX_CACHE_BREAKPOINTS - existing
    if budget <= 0:
        if upgraded > 0:
            return 0, f"ttl-upgrade({upgraded},existing={existing})"
        return 0, f"no-op(existing={existing})"

    added = 0
    parts = []

    # Strategy: inject cache_control on last block of specific messages.
    # Priority order:
    # 1. System message (if present)
    # 2. Last assistant message
    # 3. Second-to-last assistant (if exists)

    # 1. System message
    if added < budget:
        for msg in messages:
            if msg.get("role") == "system":
                blocks = _ensure_content_blocks(msg)
                if blocks and "cache_control" not in blocks[-1]:
                    blocks[-1]["cache_control"] = _new_cache_marker()
                    added += 1
                    parts.append("system")
                break

    # 2. Last assistant message (skip thinking blocks)
    if added < budget:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                blocks = _ensure_content_blocks(msg)
                # Find last non-thinking block
                for block in reversed(blocks):
                    if not isinstance(block, dict):
                        continue
                    typ = block.get("type", "text")
                    if typ in ("thinking", "redacted_thinking"):
                        continue
                    if "cache_control" not in block:
                        block["cache_control"] = _new_cache_marker()
                        added += 1
                        parts.append("last_assistant")
                    break
                break

    # 3. Second-to-last assistant (for longer conversations)
    if added < budget:
        assistant_count = 0
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                assistant_count += 1
                if assistant_count == 2:
                    blocks = _ensure_content_blocks(msg)
                    for block in reversed(blocks):
                        if not isinstance(block, dict):
                            continue
                        typ = block.get("type", "text")
                        if typ in ("thinking", "redacted_thinking"):
                            continue
                        if "cache_control" not in block:
                            block["cache_control"] = _new_cache_marker()
                            added += 1
                            parts.append("2nd_assistant")
                        break
                    break

    upg = f",upg={upgraded}" if upgraded > 0 else ""
    if added > 0:
        return added, f"{added}bp({'+'.join(parts)},{CACHE_TTL},pre={existing}{upg})"
    return 0, f"no-op(existing={existing}{upg})"


# ---------------------------------------------------------------------------
# Main Hook Class
# ---------------------------------------------------------------------------
class BedrockOptimizer(CustomLogger):
    """LiteLLM custom callback that optimizes Bedrock requests."""

    def __init__(self):
        super().__init__()
        logger.info(
            f"BedrockOptimizer initialized | cache={CACHE_ENABLED} ttl={CACHE_TTL}"
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
        ],
    ) -> Optional[dict]:
        """Modify request data before it's sent to the LLM.

        At this stage, data is in OpenAI format:
        - data["messages"]: list of {role, content} dicts
        - data["model"]: model name (our alias)

        LiteLLM's Bedrock transformation layer will later convert
        cache_control markers to Bedrock's cachePoint format.
        """
        if call_type not in ("completion", "text_completion"):
            return data

        _stats["requests"] += 1
        req_id = _stats["requests"]

        model = data.get("model", "")
        model_lower = model.lower()

        # Only apply to bedrock-bound models
        if not any(k in model_lower for k in ["claude", "anthropic", "bedrock"]):
            logger.debug(f"[#{req_id}] skip non-bedrock model: {model}")
            return data

        # --- Cache Control ---
        cache_action = "off"
        if CACHE_ENABLED:
            cache_added, cache_action = inject_cache_control(data)
            if cache_added > 0:
                _stats["cache_injected"] += 1

        logger.info(f"[#{req_id}] model={model} | cache: {cache_action}")

        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """Log cache metrics from response if available."""
        try:
            usage = getattr(response_obj, "usage", None)
            if usage:
                cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
                if cache_read > 0 or cache_write > 0:
                    model = kwargs.get("model", "unknown")
                    logger.info(
                        f"cache-metrics: model={model} read={cache_read} write={cache_write}"
                    )
        except Exception:
            pass


# Singleton instance for LiteLLM config reference
proxy_handler_instance = BedrockOptimizer()
