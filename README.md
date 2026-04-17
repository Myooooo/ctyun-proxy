# CTYun Coding Plan Proxy

将天翼云编码套餐的 API 请求进行转发代理，兼容 OpenAI API 格式，支持 chat completions 失败自动重试。同时支持对接其他 OpenAI 兼容平台（DeepSeek、Moonshot 等）。

## 功能

- **请求转发** — 透明代理到天翼云编码套餐专属地址，或任意 OpenAI 兼容 API
- **多版本兼容** — 同时支持 `/v1`、`/v2`、`/v3` 端点，处理逻辑一致
- **失败重试** — `chat/completions` 和 `messages` 接口支持自动重试（默认 5 次），429 和 5xx 自动退避
- **流式支持** — 完整支持 SSE streaming，含首包健康检查
- **模型覆盖** — 可配置默认模型强制替换请求中的模型名
- **多平台适配** — 自动从上游 URL 推导 host 头，支持配置独立 API Key
- **容错处理** — 其他接口请求失败时直接返回 HTTP 200，避免客户端异常中断

## 快速开始

### 直接运行

```bash
cp .env.example .env   # 编辑 .env 配置
pip install -r requirements.txt
python main.py
```

### Docker

```bash
docker build -t ctyun-proxy .
docker run -p 8080:8080 -v $(pwd)/.env:/app/.env ctyun-proxy
```

## 配置

通过项目根目录 `.env` 文件或环境变量配置。复制 `.env.example` 开始：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `CTYUN_BASE_URL` | 上游 API 地址 | `https://wishub-x6.ctyun.cn/coding` |
| `API_KEY` | 上游 API Key（设置后覆盖请求中的 Authorization） | 空（透传原始请求头） |
| `DEFAULT_MODEL` | 强制使用的模型名（留空则保留请求原始模型） | `GLM-5.1` |
| `MAX_RETRIES` | chat/messages 最大重试次数 | `5` |
| `REQUEST_TIMEOUT` | 单次请求超时（秒） | `600` |
| `STREAM_FIRST_CHUNK_TIMEOUT` | 流式首包超时（秒） | `60` |
| `PORT` | 服务端口 | `8080` |

## 使用场景

### 天翼云编码套餐（默认）

```env
CTYUN_BASE_URL=https://wishub-x6.ctyun.cn/coding
DEFAULT_MODEL=GLM-5.1
```

接入工具时 API Key 填写天翼云编码套餐专属 APP Key（`cp_xxx` 格式）。

### DeepSeek

```env
CTYUN_BASE_URL=https://api.deepseek.com
API_KEY=sk-xxxxxxxxxxxxxxxx
DEFAULT_MODEL=
```

### Moonshot

```env
CTYUN_BASE_URL=https://api.moonshot.cn
API_KEY=sk-xxxxxxxxxxxxxxxx
DEFAULT_MODEL=
```

## 接入 AI 工具

### Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=cp_xxxxxxxxxxxx
```

### Cursor / Cline / 其他 OpenAI 兼容工具

| 配置项 | 值 |
|---|---|
| API Base URL | `http://localhost:8080/v1` |
| API Key | `cp_xxxxxxxxxxxx`（或 `.env` 中配置 `API_KEY` 后任意填写） |

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="cp_xxxxxxxxxxxx",
)
```

## 端点说明

代理同时支持以下路径前缀，均使用相同的处理逻辑：

| 路径 | 上游转发 |
|---|---|
| `/v1/chat/completions` | `{CTYUN_BASE_URL}/v1/chat/completions` |
| `/v2/chat/completions` | `{CTYUN_BASE_URL}/v2/chat/completions` |
| `/v3/chat/completions` | `{CTYUN_BASE_URL}/v3/chat/completions` |
| `/chat/completions` | `{CTYUN_BASE_URL}/v1/chat/completions` |

## 支持的模型

所有天翼云套餐支持：GLM-5.1、GLM-5-Turbo、GLM-4.7、GLM-4.6、GLM-4.5、GLM-4.5-Air

Max/Pro 套餐额外支持：GLM-5
