# PR: Add Eager Input Streaming Support

## 📝 Summary

Adds **fine-grained tool streaming** support to the LiteLLM Bedrock optimizer by automatically injecting `eager_input_streaming: true` into tool definitions. This optimization reduces latency for large tool parameters (code blocks, long text) by streaming without buffering/JSON validation.

## 🎯 Motivation

Claude's [Fine-grained Tool Streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming) enables streaming tool inputs character-by-character, reducing latency from ~15s to ~3s for large parameters. This is critical for:

- **Agent applications** (Claude Code, Cursor, Cline)
- **Code generation tools** (write_file, patch_code)
- **Real-time UI feedback** (progress bars, live previews)

## ✨ Changes

### Core Implementation

1. **Auto-injection**: Adds `eager_input_streaming: true` to all tools in `async_pre_call_hook`
2. **Smart preservation**: Respects explicit user config (won't override `false`)
3. **Configurable**: `EAGER_INPUT_STREAMING` env var (default: `1`)

### Files Changed

```
README.md                   | 10 +++
bedrock_optimizer.py        | 48 ++++++++++--
test_optimizer.py           | 99 +++++++++++++++++++++++
example_eager_streaming.py  | 73 ++++++++++++++++  (new)
EAGER_STREAMING.md          | 215 ++++++++++++++++++++++++++  (new)
```

### Test Coverage

- ✅ 6 new test cases
- ✅ All 17 tests passing (11 existing + 6 new)
- ✅ Example script demonstrates optimization

## 📊 Performance Impact

**Before** (traditional buffered mode):
```text
Latency: ~15s
Chunk 1: '{"'
Chunk 2: 'content": "Tw'
Chunk 3: 'inkle, tw'
...
```

**After** (eager streaming):
```text
Latency: ~3s
Chunk 1: '{"content": "Twinkle, twinkle, little star,\nHow I wonder'
Chunk 2: ' what you are!\nUp above the world so high,...'
```

**5x latency reduction** for tool parameter start streaming!

## 🔧 Configuration

```bash
# Enable (default)
export EAGER_INPUT_STREAMING=1

# Disable
export EAGER_INPUT_STREAMING=0
```

## 📖 Documentation

- **EAGER_STREAMING.md**: Detailed explanation, examples, scenarios
- **example_eager_streaming.py**: Runnable demonstration
- **README.md**: Updated feature table and env vars

## ⚠️ Trade-offs

**Pros:**
- ✅ Significantly lower latency
- ✅ Better user experience for agents
- ✅ Larger, more continuous chunks
- ✅ Zero client changes required

**Cons:**
- ⚠️ May receive incomplete JSON if `max_tokens` is hit
- ⚠️ Clients must handle partial input gracefully

**Recommendation**: Enable by default. Users needing strict JSON validation can disable with `EAGER_INPUT_STREAMING=0`.

## 🧪 Testing

```bash
# Run tests
source .venv/bin/activate
python -m pytest test_optimizer.py -v

# Run example
python example_eager_streaming.py
```

## 🔗 References

- [Claude Fine-grained Tool Streaming Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming)
- [Original Go Proxy Implementation](https://github.com/KevinZhao/claudecode-bedrock-proxy)

## 📋 Checklist

- [x] Implementation complete
- [x] Tests added and passing
- [x] Documentation updated
- [x] Example provided
- [x] Backward compatible
- [x] Configurable via env var
- [x] Commits follow conventional commits

## 🚀 Ready to Merge

This PR is ready for review. The feature is production-ready with comprehensive tests and documentation.

---

**Commits:**
- `3e22053` feat: add eager_input_streaming support for fine-grained tool streaming
- `7027fb9` docs: add eager_input_streaming examples and detailed documentation
