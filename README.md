# LiteLLM Bedrock Claude Code Optimizer

将 [claudecode-bedrock-proxy](https://github.com/KevinZhao/claudecode-bedrock-proxy) 的核心优化集成到 LiteLLM Proxy，提供统一的 OpenAI 兼容 API。

## 功能

| 功能 | 说明 |
|------|------|
| **Prompt Cache 注入** | 自动为无 `cache_control` 的请求注入最多 4 个断点 |
| **Cache TTL 升级** | 5min → 1h，大幅减少 cache miss 重写费用 |
| **Thinking/Effort 优化** | Opus/Sonnet 4.6 → adaptive+max；旧模型 → budget_tokens 最大化 |
| **Model 别名映射** | `claude-opus-4-6` → `bedrock/global.anthropic.claude-opus-4-6-v1` |
| **自动版本升级** | `claude-sonnet-4-5` 请求自动路由到 Sonnet 4.6 |
| **统一 API** | OpenAI ChatCompletions 格式，兼容 Claude Code / Cursor / Cline 等所有客户端 |

## 架构

```
Client (OpenAI API)
  │
  ▼
LiteLLM Proxy (:4000)
  ├─ async_pre_call_hook (bedrock_optimizer.py)
  │   ├─ cache_control 注入 / TTL 升级
  │   ├─ thinking/effort 优化
  │   └─ strip defer_loading
  │
  ▼
AWS Bedrock (via litellm bedrock provider)
```

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install 'litellm[proxy]' boto3
```

### 2. 配置 AWS Credentials

```bash
aws configure
# 或使用环境变量
export AWS_ACCESS_KEY_ID=xxx
export AWS_SECRET_ACCESS_KEY=xxx
export AWS_REGION_NAME=us-east-1
```

### 3. 启动

```bash
./start.sh
```

### 4. 测试

```bash
curl http://localhost:4000/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## 文件结构

```
├── bedrock_optimizer.py   # 核心优化逻辑（LiteLLM async_pre_call_hook）
├── config.yaml            # LiteLLM 配置（模型列表 + callback 注册）
├── test_optimizer.py      # 单元测试（13 cases）
├── start.sh               # 启动脚本
└── requirements.txt       # Python 依赖
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CACHE_ENABLED` | `1` | 启用 prompt caching 注入 |
| `CACHE_TTL` | `1h` | Cache TTL（`5m` 或 `1h`） |
| `AWS_REGION_NAME` | - | AWS 区域 |

## 优化细节

### Prompt Cache

- **TTL 升级**：将所有已有 `cache_control` 的 TTL 从 5min 升级为 1h
- **自动注入**：对无 breakpoints 的请求（agent-sdk 场景），按优先级注入：
  1. 最后一个 tool
  2. System prompt
  3. 最后一条 assistant message 的最后一个非 thinking block
- 遵守 API 限制：最多 4 个断点

### Thinking/Effort

| 模型 | 策略 |
|------|------|
| Opus 4.6 / Sonnet 4.6 | `thinking: adaptive` + `effort: max` + `context-1m` beta |
| 旧模型（Sonnet 4.5 等） | `budget_tokens` 最大化至 `max_tokens - 1` |
| Haiku | 跳过（不支持 thinking） |

## 对比原 Go Proxy

| 维度 | Go proxy | LiteLLM 集成 |
|------|----------|-------------|
| API 格式 | Anthropic | ✅ OpenAI（通用） |
| 多 provider | Bedrock only | ✅ 100+ |
| Cost tracking | 基础日志 | ✅ 内置 |
| Load balancing | 无 | ✅ 多策略 |
| 部署 | 单 binary | Python venv |
| HTTP/2 upstream | ✅ Go 原生 | ⚠️ 取决于 boto3 |

## 运行测试

```bash
source .venv/bin/activate
pip install pytest
python -m pytest test_optimizer.py -v
```

## 参考

- [claudecode-bedrock-proxy](https://github.com/KevinZhao/claudecode-bedrock-proxy) — 原始 Go 实现
- [LiteLLM Docs](https://docs.litellm.ai) — LiteLLM 文档
- [LiteLLM Custom Hooks](https://docs.litellm.ai/docs/proxy/call_hooks) — 自定义 Callback 机制

## License

MIT
