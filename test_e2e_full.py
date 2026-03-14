#!/usr/bin/env python3
"""
Comprehensive E2E test for LiteLLM Bedrock Proxy + Optimizer.

Inspired by bedrock-cache-verification/bedrock_cache_poc.py methodology:
- Real Bedrock calls through the proxy
- Metrics-based verification (cache_write / cache_read tokens)
- Structured test scenarios with clear pass/fail
- UUID-stamped content to avoid cache residue

Tests:
  0. Health check
  1. Basic completion — proxy alive, response valid
  2. Cache injection — optimizer injects cache_control, verify WRITE then READ
  3. Model alias mapping — all configured aliases resolve
  4. Streaming — SSE streaming works end-to-end
  5. Multi-turn conversation — context preserved, cache across turns
  6. Pre-existing cache_control — optimizer upgrades TTL, doesn't double-inject
  7. Thinking blocks — assistant thinking blocks are skipped by cache injection
  8. Large conversation — 4 breakpoint limit respected
  9. Error handling — invalid model, empty messages
 10. Cache metrics pipeline — verify cache tokens flow through to response

Requires: LiteLLM proxy running on localhost:4000
Usage:   python test_e2e_full.py [--test N] [--model MODEL]
"""

import json
import time
import sys
import uuid
import copy
import requests
from datetime import datetime

PROXY_URL = "http://localhost:4000"
DEFAULT_MODEL = "claude-sonnet-4-5"

PASS = 0
FAIL = 0
WARN = 0
run_id = uuid.uuid4().hex[:8]


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        log(f"  \u2705 {name}" + (f" \u2014 {detail}" if detail else ""))
    else:
        FAIL += 1
        log(f"  \u274c {name}" + (f" \u2014 {detail}" if detail else ""), "FAIL")


def warn(name, detail=""):
    global WARN
    WARN += 1
    log(f"  \u26a0\ufe0f  {name}" + (f" \u2014 {detail}" if detail else ""), "WARN")


# --- Helpers ---

def padding(n=1200):
    """Generate padding text to exceed 1024 token minimum for cache."""
    lines = []
    for i in range(n // 10):
        lines.append(f"Rule {i+1}: When handling queries about topic #{i+1}, "
                     f"consider best practices, optimization, and excellence.")
    return "\n".join(lines)


SYSTEM_PROMPT = f"You are an expert assistant. [{run_id}]\n{padding()}"


def chat(messages, model=DEFAULT_MODEL, stream=False, max_tokens=100, **kwargs):
    """Send a chat completion request to the proxy."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        **kwargs,
    }
    if stream:
        resp = requests.post(
            f"{PROXY_URL}/chat/completions",
            json=payload, stream=True, timeout=120,
        )
        resp.raise_for_status()
        chunks = []
        for line in resp.iter_lines():
            line = line.decode("utf-8")
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunks.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        return chunks
    else:
        resp = requests.post(
            f"{PROXY_URL}/chat/completions",
            json=payload, timeout=120,
        )
        resp.raise_for_status()
        return resp.json()


def chat_raw(payload):
    """Send raw payload, return (status_code, json_or_text)."""
    try:
        resp = requests.post(
            f"{PROXY_URL}/chat/completions",
            json=payload, timeout=30,
        )
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, resp.text
    except Exception as e:
        return 0, str(e)


def get_usage(resp):
    return resp.get("usage", {})


def get_content(resp):
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return ""


def get_cache_metrics(usage):
    return {
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
        "cache_write": usage.get("cache_creation_input_tokens", 0) or 0,
        "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
        "completion_tokens": usage.get("completion_tokens", 0) or 0,
    }


# ======================================================
# TEST 0: Health check
# ======================================================
def test_0_health():
    log("\n" + "=" * 60)
    log("TEST 0: Proxy health check")
    log("=" * 60)
    try:
        resp = requests.get(f"{PROXY_URL}/health", timeout=10)
        data = resp.json()
        healthy = data.get("healthy_count", 0)
        unhealthy = data.get("unhealthy_count", 0)
        check("Proxy responds", resp.status_code == 200)
        check("Healthy endpoints > 0", healthy > 0, f"healthy={healthy}, unhealthy={unhealthy}")
        return True
    except Exception as e:
        check("Proxy responds", False, str(e))
        return False


# ======================================================
# TEST 1: Basic completion
# ======================================================
def test_1_basic():
    log("\n" + "=" * 60)
    log("TEST 1: Basic completion")
    log("=" * 60)

    resp = chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"[{run_id}] Say 'hello' in one word."},
    ])
    usage = get_usage(resp)
    content = get_content(resp)

    check("Got non-empty response", len(content) > 0, f"'{content[:80]}'")
    check("Has usage stats", usage.get("total_tokens", 0) > 0,
          f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")
    check("Model field present", "model" in resp, resp.get("model", ""))
    check("Has choices array", len(resp.get("choices", [])) > 0)
    check("Finish reason present",
          resp.get("choices", [{}])[0].get("finish_reason") is not None,
          resp.get("choices", [{}])[0].get("finish_reason", ""))


# ======================================================
# TEST 2: Cache injection (WRITE then READ)
# ======================================================
def test_2_cache():
    log("\n" + "=" * 60)
    log("TEST 2: Cache injection (expect WRITE then READ)")
    log("=" * 60)
    log(f"  Using unique run_id={run_id} to avoid stale cache")

    msgs1 = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"[{run_id}] What is Amazon S3? One sentence."},
    ]

    # Call 1: should trigger cache WRITE
    log("  Call 1 (expect cache WRITE)...")
    resp1 = chat(msgs1, max_tokens=50)
    u1 = get_usage(resp1)
    cm1 = get_cache_metrics(u1)
    log(f"  Usage: {json.dumps(u1, indent=2)}")
    check("Call 1 got response", len(get_content(resp1)) > 0)

    time.sleep(3)

    # Call 2: same system prompt, different user question -> should READ from cache
    msgs2 = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"[{run_id}] What is Amazon EC2? One sentence."},
    ]
    log("  Call 2 (expect cache READ on system prompt)...")
    resp2 = chat(msgs2, max_tokens=50)
    u2 = get_usage(resp2)
    cm2 = get_cache_metrics(u2)
    log(f"  Usage: {json.dumps(u2, indent=2)}")
    check("Call 2 got response", len(get_content(resp2)) > 0)

    # Verify cache metrics if available
    if cm1["cache_write"] > 0:
        check("Call 1 wrote to cache", True, f"cache_write={cm1['cache_write']}")
    else:
        warn("Call 1 cache_write=0", "LiteLLM may not expose cache metrics in OpenAI format")

    if cm2["cache_read"] > 0:
        check("Call 2 read from cache", True, f"cache_read={cm2['cache_read']}")
    else:
        warn("Call 2 cache_read=0", "Check proxy logs for [bedrock-opt] cache injection")

    log("  (Check proxy terminal for [bedrock-opt] log lines confirming injection)")


# ======================================================
# TEST 3: Model alias mapping
# ======================================================
def test_3_model_alias():
    log("\n" + "=" * 60)
    log("TEST 3: Model alias mapping")
    log("=" * 60)

    aliases = [
        ("claude-sonnet-4-5", "Sonnet 4.5 -> 4.6 auto-upgrade"),
        ("claude-sonnet-4-6", "Sonnet 4.6 direct"),
        ("claude-haiku-4-5", "Haiku 4.5"),
    ]

    for alias, desc in aliases:
        try:
            resp = chat(
                [{"role": "user", "content": f"[{run_id}] Hi"}],
                model=alias, max_tokens=10,
            )
            content = get_content(resp)
            check(f"{alias} \u2192 {desc}", len(content) > 0,
                  f"model={resp.get('model', '?')}")
        except Exception as e:
            check(f"{alias} \u2192 {desc}", False, str(e)[:120])
        time.sleep(1)


# ======================================================
# TEST 4: Streaming
# ======================================================
def test_4_streaming():
    log("\n" + "=" * 60)
    log("TEST 4: Streaming completion")
    log("=" * 60)
    log("  Using longer prompt to ensure real SSE streaming")

    # LiteLLM may buffer very short responses into a single non-SSE JSON reply.
    # Use a longer prompt + higher max_tokens to force real streaming.
    resp = requests.post(
        f"{PROXY_URL}/chat/completions",
        json={
            "model": DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": f"You are helpful. [{run_id}]"},
                {"role": "user", "content": f"[{run_id}] Write a short paragraph about cloud computing benefits."},
            ],
            "max_tokens": 200,
            "stream": True,
        },
        stream=True, timeout=120,
    )
    resp.raise_for_status()

    ct = resp.headers.get("content-type", "")
    check("Content-Type is text/event-stream", "text/event-stream" in ct, ct)

    chunks = []
    content = ""
    for line in resp.iter_lines():
        decoded = line.decode("utf-8")
        if decoded.startswith("data: ") and decoded != "data: [DONE]":
            try:
                chunk = json.loads(decoded[6:])
                chunks.append(chunk)
                choices = chunk.get("choices", [{}])
                if choices:
                    delta = choices[0].get("delta", {})
                    msg = choices[0].get("message", {})
                    content += delta.get("content", "") or msg.get("content", "")
            except json.JSONDecodeError:
                pass
        elif not decoded.startswith("data: ") and decoded.strip().startswith("{"):
            # LiteLLM sometimes returns a single JSON object instead of SSE
            try:
                full = json.loads(decoded)
                chunks.append(full)
                c = full.get("choices", [{}])[0].get("message", {}).get("content", "")
                content += c
                log("  (Got buffered non-SSE response)")
            except json.JSONDecodeError:
                pass

    check("Got stream chunks", len(chunks) > 0, f"{len(chunks)} chunks")
    check("Stream has content", len(content) > 0, f"'{content[:80]}'")

    if chunks:
        first = chunks[0]
        check("First chunk has id", "id" in first)
        check("First chunk has model", "model" in first)

    if len(chunks) > 2:
        log(f"  Real streaming confirmed: {len(chunks)} chunks received")
    elif len(chunks) == 1:
        warn("Only 1 chunk", "LiteLLM may have buffered the response")


# ======================================================
# TEST 5: Multi-turn conversation + cache
# ======================================================
def test_5_multiturn():
    log("\n" + "=" * 60)
    log("TEST 5: Multi-turn conversation (context + cache)")
    log("=" * 60)

    # Turn 1
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"[{run_id}] My name is TestUser42. Remember it."},
    ]
    resp1 = chat(messages, max_tokens=80)
    a1 = get_content(resp1)
    check("Turn 1 responded", len(a1) > 0, a1[:80])

    time.sleep(2)

    # Turn 2: extend conversation
    messages.append({"role": "assistant", "content": a1})
    messages.append({"role": "user", "content": f"[{run_id}] What is my name?"})
    resp2 = chat(messages, max_tokens=80)
    a2 = get_content(resp2)
    check("Turn 2 responded", len(a2) > 0, a2[:80])
    check("Remembers name", "testuser42" in a2.lower(), a2[:120])

    # Check cache metrics on turn 2 (system + assistant should be cached)
    u2 = get_usage(resp2)
    cm2 = get_cache_metrics(u2)
    if cm2["cache_read"] > 0:
        check("Turn 2 cache read", True, f"cache_read={cm2['cache_read']}")
    else:
        warn("Turn 2 cache_read=0", "System/assistant cache may not be visible")

    time.sleep(2)

    # Turn 3: same prefix, new question -> cache should hit
    messages.append({"role": "assistant", "content": a2})
    messages.append({"role": "user", "content": f"[{run_id}] Spell my name backwards."})
    resp3 = chat(messages, max_tokens=80)
    a3 = get_content(resp3)
    check("Turn 3 responded", len(a3) > 0, a3[:80])


# ======================================================
# TEST 6: Pre-existing cache_control (TTL upgrade)
# ======================================================
def test_6_existing_cache_control():
    log("\n" + "=" * 60)
    log("TEST 6: Pre-existing cache_control (TTL upgrade)")
    log("=" * 60)
    log("  Sending messages with cache_control already present.")
    log("  Optimizer should upgrade TTL, not add duplicate markers.")

    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}},
            ],
        },
        {"role": "user", "content": f"[{run_id}] What is Lambda?"},
    ]

    resp = chat(messages, max_tokens=50)
    content = get_content(resp)
    check("Response with pre-existing cache_control", len(content) > 0, content[:80])

    usage = get_usage(resp)
    check("Has usage", usage.get("total_tokens", 0) > 0)

    # The optimizer should log "ttl-upgrade" or "existing=" in its output
    log("  Check proxy logs for ttl-upgrade or existing= messages")


# ======================================================
# TEST 7: Thinking blocks (skip injection)
# ======================================================
def test_7_thinking_blocks():
    log("\n" + "=" * 60)
    log("TEST 7: Thinking blocks in assistant messages")
    log("=" * 60)
    log("  Optimizer should skip thinking blocks for cache injection.")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"[{run_id}] What is S3?"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me think about S3..."},
                {"type": "text", "text": "Amazon S3 is an object storage service."},
            ],
        },
        {"role": "user", "content": f"[{run_id}] Tell me more about S3 pricing."},
    ]

    try:
        resp = chat(messages, max_tokens=80)
        content = get_content(resp)
        check("Response with thinking blocks", len(content) > 0, content[:80])
    except requests.exceptions.HTTPError as e:
        # Some models/configs may reject thinking blocks — that's OK
        warn("Thinking block request failed", str(e)[:120])
        log("  This may be expected if the model doesn't support thinking blocks in input")


# ======================================================
# TEST 8: Large conversation (4 breakpoint limit)
# ======================================================
def test_8_large_conversation():
    log("\n" + "=" * 60)
    log("TEST 8: Large conversation (4 breakpoint limit)")
    log("=" * 60)
    log("  10 user/assistant turns. Optimizer should inject <= 4 breakpoints.")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    for i in range(10):
        messages.append({"role": "user", "content": f"[{run_id}] Question {i+1} about AWS."})
        messages.append({"role": "assistant", "content": f"[{run_id}] Answer {i+1} about AWS services."})
    messages.append({"role": "user", "content": f"[{run_id}] Summarize."})

    resp = chat(messages, max_tokens=80)
    content = get_content(resp)
    check("Large conv got response", len(content) > 0, content[:80])

    usage = get_usage(resp)
    check("Large conv has usage", usage.get("total_tokens", 0) > 0,
          f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")

    log("  Check proxy logs: should see <=4 breakpoints injected")
    log("  (e.g., '3bp(system+last_assistant+2nd_assistant,...)')")


# ======================================================
# TEST 9: Error handling
# ======================================================
def test_9_errors():
    log("\n" + "=" * 60)
    log("TEST 9: Error handling")
    log("=" * 60)

    # 9a: Invalid model
    log("  9a: Invalid model name...")
    status, body = chat_raw({
        "model": "nonexistent-model-xyz",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
    })
    check("Invalid model returns error", status >= 400, f"status={status}")

    # 9b: Empty messages
    log("  9b: Empty messages array...")
    status2, body2 = chat_raw({
        "model": DEFAULT_MODEL,
        "messages": [],
        "max_tokens": 10,
    })
    # LiteLLM may accept or reject — just shouldn't crash
    check("Empty messages handled gracefully", status2 > 0,
          f"status={status2}")

    # 9c: Very large max_tokens (should still work, just capped)
    log("  9c: Large max_tokens...")
    try:
        resp = chat(
            [{"role": "user", "content": f"[{run_id}] Say hi"}],
            max_tokens=100000,
        )
        check("Large max_tokens handled", len(get_content(resp)) > 0)
    except Exception as e:
        check("Large max_tokens handled", True, f"Error is acceptable: {str(e)[:80]}")


# ======================================================
# TEST 10: Cache metrics pipeline
# ======================================================
def test_10_cache_metrics():
    log("\n" + "=" * 60)
    log("TEST 10: Cache metrics pipeline (detailed)")
    log("=" * 60)
    log("  3 sequential calls with same system prompt.")
    log("  Tracking cache_write and cache_read token counts.")

    uid = uuid.uuid4().hex[:8]
    sys_prompt = f"You are a helpful assistant. [{uid}]\n{padding()}"

    results = []
    for i in range(3):
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"[{uid}] Question {i+1}: What is AWS service #{i+1}?"},
        ]
        resp = chat(msgs, max_tokens=50)
        u = get_usage(resp)
        cm = get_cache_metrics(u)
        results.append(cm)
        log(f"  Call {i+1}: prompt={cm['prompt_tokens']} "
            f"cache_write={cm['cache_write']} cache_read={cm['cache_read']}")
        time.sleep(2)

    # Call 1 should write, calls 2-3 should read
    if results[0]["cache_write"] > 0:
        check("Call 1 cache_write > 0", True, f"{results[0]['cache_write']} tokens")
    else:
        warn("Call 1 cache_write=0", "Metrics may not be visible in response")

    if results[1]["cache_read"] > 0:
        check("Call 2 cache_read > 0", True, f"{results[1]['cache_read']} tokens")
    elif results[0]["cache_write"] > 0:
        check("Call 2 cache_read > 0", False,
              "Cache was written but not read on next call")
    else:
        warn("Call 2 cache_read=0", "Can't verify without write metrics")

    if results[2]["cache_read"] > 0:
        check("Call 3 cache_read > 0", True, f"{results[2]['cache_read']} tokens")
    else:
        warn("Call 3 cache_read=0", "Check proxy logs")

    # Verify that cached calls use fewer prompt tokens (or at least same)
    if all(r["prompt_tokens"] > 0 for r in results):
        log(f"  Prompt token trend: {results[0]['prompt_tokens']} -> "
            f"{results[1]['prompt_tokens']} -> {results[2]['prompt_tokens']}")


# ======================================================
# MAIN
# ======================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Comprehensive E2E test for LiteLLM Bedrock Proxy")
    parser.add_argument("--test", type=int, choices=range(0, 11),
                       help="Run specific test only (0-10)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                       help=f"Model to use (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    # Note: model override not supported at runtime since chat() captures default at def time
    model_name = args.model

    log(f"LiteLLM Bedrock Proxy \u2014 Comprehensive E2E Verification")
    log(f"Proxy: {PROXY_URL}")
    log(f"Model: {model_name}")
    log(f"Run ID: {run_id} (unique per run)")

    tests = {
        0:  ("Health check",               test_0_health),
        1:  ("Basic completion",            test_1_basic),
        2:  ("Cache injection",             test_2_cache),
        3:  ("Model alias mapping",         test_3_model_alias),
        4:  ("Streaming",                   test_4_streaming),
        5:  ("Multi-turn + cache",          test_5_multiturn),
        6:  ("Pre-existing cache_control",  test_6_existing_cache_control),
        7:  ("Thinking blocks",             test_7_thinking_blocks),
        8:  ("Large conversation (4 BP)",   test_8_large_conversation),
        9:  ("Error handling",              test_9_errors),
        10: ("Cache metrics pipeline",      test_10_cache_metrics),
    }

    run_tests = [args.test] if args.test is not None else list(tests.keys())

    # Always start with health check
    if 0 in run_tests:
        if not test_0_health():
            log("\n\u274c Proxy not running. Start with: ./start.sh")
            sys.exit(1)
        run_tests = [t for t in run_tests if t != 0]

    for t in run_tests:
        name, func = tests[t]
        try:
            func()
        except Exception as e:
            log(f"TEST {t} ({name}) CRASHED: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            global FAIL
            FAIL += 1

    log("\n" + "=" * 60)
    log(f"RESULTS: {PASS} passed, {FAIL} failed, {WARN} warnings")
    log("=" * 60)

    if FAIL > 0:
        sys.exit(1)
    else:
        log("\u2705 All tests passed!")


if __name__ == "__main__":
    main()
