"""
Bedrock Optimizer — LiteLLM Custom Callback Hook

Ports the key optimizations from claudecode-bedrock-proxy (Go) into a
LiteLLM async_pre_call_hook:

1. Prompt Cache TTL upgrade (5m → 1h)
2. Prompt Cache breakpoint auto-injection (up to 4)
3. Thinking / effort optimization (adaptive+max for new models, budget_tokens max for legacy)
4. Strip unsupported fields (defer_loading)

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
DEFAULT_MAX_BUDGET = 128_000

# Models that support adaptive thinking + effort
ADAPTIVE_MODELS = ["opus-4-6", "sonnet-4-6"]
# Models where we skip thinking injection entirely
THINKING_SKIP_MODELS = ["haiku"]

# Stats (simple counters, not atomic but good enough for logging)
_stats = {
    "requests": 0,
    "thinking_modified": 0,
    "cache_injected": 0,
}


def _contains_any(s: str, subs: List[str]) -> bool:
    return any(sub in s for sub in subs)


def _new_cache_marker() -> Dict[str, Any]:
    marker: Dict[str, Any] = {"type": "ephemeral"}
    if CACHE_TTL != "5m":
        marker["ttl"] = CACHE_TTL
    return marker


# ---------------------------------------------------------------------------
# Thinking / Effort Modification
# ---------------------------------------------------------------------------
def modify_thinking(data: Dict[str, Any], model_lower: str) -> Tuple[bool, str]:
    """Modify thinking config based on model type."""
    if _contains_any(model_lower, THINKING_SKIP_MODELS):
        return False, "skip (haiku)"

    if "messages" not in data:
        return False, "passthrough (no messages)"

    if _contains_any(model_lower, ADAPTIVE_MODELS):
        return _modify_adaptive_thinking(data)

    return _modify_legacy_thinking(data)


def _modify_adaptive_thinking(data: Dict[str, Any]) -> Tuple[bool, str]:
    """For Opus 4.6 / Sonnet 4.6: set adaptive thinking + effort=max + context-1m beta."""
    changes: List[str] = []

    # Thinking → adaptive
    thinking = data.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "adaptive":
        old = str(thinking)
        data["thinking"] = {"type": "adaptive"}
        changes.append(f"thinking->adaptive (was {old})")

    # effort → max
    oc = data.get("output_config")
    if not isinstance(oc, dict):
        oc = {}
        data["output_config"] = oc
    if oc.get("effort") != "max":
        old_effort = oc.get("effort", "unset")
        oc["effort"] = "max"
        changes.append(f"effort->max (was {old_effort})")

    # Context-1m beta
    context_beta = "context-1m-2025-08-07"
    betas = data.get("anthropic_beta", [])
    if not isinstance(betas, list):
        betas = []
    if context_beta not in betas:
        betas.append(context_beta)
        data["anthropic_beta"] = betas
        changes.append(f"beta+={context_beta}")

    if changes:
        return True, "; ".join(changes)
    return False, "already adaptive+max"


def _modify_legacy_thinking(data: Dict[str, Any]) -> Tuple[bool, str]:
    """For older models (Sonnet 4.5 etc): maximize budget_tokens."""
    max_tokens = data.get("max_tokens", DEFAULT_MAX_BUDGET)
    if not isinstance(max_tokens, (int, float)):
        max_tokens = DEFAULT_MAX_BUDGET
    target_budget = int(max_tokens) - 1

    thinking = data.get("thinking")

    # No thinking or disabled → inject
    if thinking is None or (isinstance(thinking, dict) and thinking.get("type") == "disabled"):
        data["thinking"] = {
            "type": "enabled",
            "budget_tokens": target_budget,
        }
        thinking_beta = "interleaved-thinking-2025-05-14"
        betas = data.get("anthropic_beta", [])
        if not isinstance(betas, list):
            betas = []
        if thinking_beta not in betas:
            betas.append(thinking_beta)
            data["anthropic_beta"] = betas
        return True, f"injected budget={target_budget}"

    # Existing thinking → upgrade budget
    if isinstance(thinking, dict):
        old_budget = int(thinking.get("budget_tokens", 0))
        if old_budget < target_budget:
            thinking["budget_tokens"] = target_budget
            return True, f"upgraded {old_budget}->{target_budget}"
        return False, f"already max budget={old_budget}"

    return False, "skip (unexpected format)"


# ---------------------------------------------------------------------------
# Cache Control Injection
# ---------------------------------------------------------------------------
def _collect_existing_cache_blocks(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect all blocks that already have cache_control."""
    blocks: List[Dict[str, Any]] = []

    def _collect_from_list(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and "cache_control" in item:
                blocks.append(item)

    _collect_from_list(data.get("tools"))
    _collect_from_list(data.get("system"))

    messages = data.get("messages", [])
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                _collect_from_list(msg.get("content"))

    return blocks


def inject_cache_control(data: Dict[str, Any]) -> Tuple[int, str]:
    """Inject cache_control breakpoints and upgrade TTL."""
    blocks = _collect_existing_cache_blocks(data)
    existing = len(blocks)

    # Upgrade TTL on existing blocks
    upgraded = 0
    if CACHE_TTL != "5m":
        for block in blocks:
            cc = block.get("cache_control")
            if isinstance(cc, dict):
                cc["ttl"] = CACHE_TTL
                upgraded += 1

    budget = MAX_CACHE_BREAKPOINTS - existing
    added = 0
    parts: List[str] = []

    if budget <= 0:
        if upgraded > 0:
            return 0, f"ttl-upgrade({upgraded}->{CACHE_TTL},existing={existing})"
        return 0, f"no-op(existing={existing})"

    # 1. Last tool
    tools = data.get("tools")
    if isinstance(tools, list) and len(tools) > 0 and added < budget:
        last = tools[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = _new_cache_marker()
            added += 1
            parts.append("tools")

    # 2. System prompt
    if added < budget:
        system = data.get("system")
        if isinstance(system, str) and system:
            data["system"] = [
                {"type": "text", "text": system, "cache_control": _new_cache_marker()}
            ]
            added += 1
            parts.append("system")
        elif isinstance(system, list) and len(system) > 0:
            last = system[-1]
            if isinstance(last, dict) and "cache_control" not in last:
                last["cache_control"] = _new_cache_marker()
                added += 1
                parts.append("system")

    # 3. Last assistant turn's last non-thinking block
    if added < budget:
        messages = data.get("messages", [])
        if isinstance(messages, list):
            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i]
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    msg["content"] = [
                        {"type": "text", "text": content, "cache_control": _new_cache_marker()}
                    ]
                    added += 1
                    parts.append("msgs")
                elif isinstance(content, list) and len(content) > 0:
                    for j in range(len(content) - 1, -1, -1):
                        block = content[j]
                        if not isinstance(block, dict):
                            continue
                        typ = block.get("type", "")
                        if typ in ("thinking", "redacted_thinking"):
                            continue
                        if "cache_control" not in block:
                            block["cache_control"] = _new_cache_marker()
                            added += 1
                            parts.append("msgs")
                        break
                break

    upg = f",upg={upgraded}" if upgraded > 0 else ""
    if added > 0:
        return added, f"{added}bp({'+'.join(parts)},{CACHE_TTL},pre={existing}{upg})"
    return 0, f"no-op(existing={existing}{upg})"


# ---------------------------------------------------------------------------
# Strip unsupported fields
# ---------------------------------------------------------------------------
def strip_unsupported_fields(data: Dict[str, Any]) -> bool:
    """Remove fields not supported by Bedrock (e.g. defer_loading on tools)."""
    modified = False
    tools = data.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and "defer_loading" in tool:
                del tool["defer_loading"]
                modified = True
    return modified


# ---------------------------------------------------------------------------
# Main Hook Class
# ---------------------------------------------------------------------------
class BedrockOptimizer(CustomLogger):
    """LiteLLM custom callback that optimizes Bedrock requests."""

    def __init__(self):
        super().__init__()
        logger.info(
            f"BedrockOptimizer initialized | cache={CACHE_ENABLED} ttl={CACHE_TTL} "
            f"adaptive_models={ADAPTIVE_MODELS}"
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
        """Modify request data before it's sent to the LLM."""
        # Only process completion calls
        if call_type not in ("completion", "text_completion"):
            return data

        _stats["requests"] += 1
        req_id = _stats["requests"]

        model = data.get("model", "")
        model_lower = model.lower()

        # Only apply to bedrock models
        if "bedrock" not in model_lower and not _contains_any(
            model_lower,
            ["claude", "anthropic"],
        ):
            logger.debug(f"[#{req_id}] skip non-bedrock model: {model}")
            return data

        # --- Strip unsupported fields ---
        strip_unsupported_fields(data)

        # --- Thinking / Effort ---
        think_mod, think_action = modify_thinking(data, model_lower)
        if think_mod:
            _stats["thinking_modified"] += 1

        # --- Cache Control ---
        cache_action = "off"
        if CACHE_ENABLED and "messages" in data:
            cache_added, cache_action = inject_cache_control(data)
            if cache_added > 0:
                _stats["cache_injected"] += 1

        logger.info(
            f"[#{req_id}] model={model} | thinking: {think_action} | cache: {cache_action}"
        )

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
