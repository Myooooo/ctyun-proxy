# CTYun Coding Plan Proxy

将天翼云编码套餐的 API 请求进行转发代理，兼容 OpenAI API 格式，支持 chat completions 失败自动重试。

## 功能

- **请求转发** — 透明代理到天翼云编码套餐专属地址
- **失败重试** — `chat/completions` 和 `messages` 接口支持自动重试（默认 5 次），429 和 5xx 自动退避
- **流式支持** — 完整支持 SSE streaming
- **容错处理** — 其他接口请求失败时直接返回 HTTP 200，避免客户端异常中断

## 快速开始

### 直接运行

```bash
pip install -r requirements.txt
python main.py
```

### Docker

```bash
docker build -t ctyun-proxy .
docker run -p 8080:8080 ctyun-proxy
```

## 配置

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `MAX_RETRIES` | chat/messages 最大重试次数 | `5` |
| `REQUEST_TIMEOUT` | 单次请求超时（秒） | `300` |
| `PORT` | 服务端口 | `8080` |
| `CTYUN_BASE_URL` | 天翼云上游地址 | `https://wishub-x6.ctyun.cn/coding` |

## 接入 AI 工具

启动代理后，将工具的 API 地址指向本代理，API Key 填写天翼云编码套餐专属 APP Key（`cp_xxx` 格式）。

### Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=cp_xxxxxxxxxxxx
```

### Cursor / Cline / 其他 OpenAI 兼容工具

| 配置项 | 值 |
|---|---|
| API Base URL | `http://localhost:8080/v1` |
| API Key | `cp_xxxxxxxxxxxx` |

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="cp_xxxxxxxxxxxx",
)
```

## 支持的模型

所有套餐支持：GLM-5.1、GLM-5-Turbo、GLM-4.7、GLM-4.6、GLM-4.5、GLM-4.5-Air

Max/Pro 套餐额外支持：GLM-5
