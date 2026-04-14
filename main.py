import asyncio
import json
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ctyun-proxy")

app = FastAPI(title="CTYun Coding Plan Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CTYUN_BASE_URL = os.getenv("CTYUN_BASE_URL", "https://wishub-x6.ctyun.cn/coding")
DEFAULT_MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "300"))
PORT = int(os.getenv("PORT", "8080"))

HOP_BY_HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-length",
}


def _forward_headers(request: Request) -> dict:
    headers = {}
    for k, v in request.headers.items():
        if k.lower() not in HOP_BY_HOP:
            headers[k] = v
    headers["host"] = "wishub-x6.ctyun.cn"
    return headers


def _is_chat_completions(path: str) -> bool:
    return "chat/completions" in path


def _is_messages(path: str) -> bool:
    segments = path.split("/")
    return segments and segments[-1].split("?")[0] == "messages"


def _parse_stream_flag(body: bytes) -> bool:
    try:
        return json.loads(body).get("stream", False)
    except (json.JSONDecodeError, ValueError):
        return False


def _clean_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


async def _backoff(attempt: int):
    await asyncio.sleep(min(2 ** (attempt - 1), 30))


async def _stream_response(client: httpx.AsyncClient, method: str, url: str, headers: dict, body: bytes):
    async with client.stream(method, url, headers=headers, content=body) as resp:
        async def generate():
            async for chunk in resp.aiter_bytes():
                yield chunk
        return StreamingResponse(generate(), status_code=resp.status_code, headers=_clean_headers(resp.headers))


async def _retry_request(target_url: str, headers: dict, body: bytes, method: str, max_retries: int):
    """Retry logic for chat completions and messages endpoints."""
    is_stream = _parse_stream_flag(body)
    last_exc: Exception | None = None
    last_resp: httpx.Response | None = None

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT)) as client:
                if is_stream:
                    return await _stream_response(client, method, target_url, headers, body)

                resp = await client.request(method, target_url, headers=headers, content=body)
                should_retry = resp.status_code == 429 or resp.status_code >= 500

                if should_retry and attempt < max_retries:
                    logger.warning(f"Attempt {attempt}/{max_retries} got {resp.status_code}, retrying...")
                    last_resp = resp
                    await _backoff(attempt)
                    continue

                return Response(content=resp.content, status_code=resp.status_code, headers=_clean_headers(resp.headers))

        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                logger.warning(f"Attempt {attempt}/{max_retries} error: {exc}, retrying...")
                await _backoff(attempt)
                continue

    # All retries exhausted
    if last_resp is not None:
        return Response(content=last_resp.content, status_code=last_resp.status_code, headers=_clean_headers(last_resp.headers))
    return JSONResponse(status_code=502, content={"error": {"message": str(last_exc), "type": "proxy_error"}})


async def _simple_proxy(target_url: str, headers: dict, body: bytes, method: str):
    """Non-retry proxy: on failure return HTTP 200 with error info."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT)) as client:
            resp = await client.request(method, target_url, headers=headers, content=body)
            return Response(content=resp.content, status_code=resp.status_code, headers=_clean_headers(resp.headers))
    except Exception as exc:
        logger.error(f"Non-critical request failed: {exc}")
        return JSONResponse(status_code=200, content={"error": {"message": str(exc), "type": "proxy_error"}})


# ── Routes ──────────────────────────────────────────────────────────────────

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v1(request: Request, path: str):
    target_url = f"{CTYUN_BASE_URL}/v1/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = _forward_headers(request)
    body = await request.body()

    if _is_chat_completions(path):
        logger.info(f"→ chat/completions (retry={DEFAULT_MAX_RETRIES})")
        return await _retry_request(target_url, headers, body, request.method, DEFAULT_MAX_RETRIES)

    if _is_messages(path):
        logger.info(f"→ messages (retry={DEFAULT_MAX_RETRIES})")
        return await _retry_request(target_url, headers, body, request.method, DEFAULT_MAX_RETRIES)

    logger.info(f"→ {path}")
    return await _simple_proxy(target_url, headers, body, request.method)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_root(request: Request, path: str):
    if path in ("docs", "openapi.json", "redoc"):
        return

    target_url = f"{CTYUN_BASE_URL}/v1/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = _forward_headers(request)
    body = await request.body()

    if _is_chat_completions(path):
        return await _retry_request(target_url, headers, body, request.method, DEFAULT_MAX_RETRIES)

    if _is_messages(path):
        return await _retry_request(target_url, headers, body, request.method, DEFAULT_MAX_RETRIES)

    return await _simple_proxy(target_url, headers, body, request.method)


@app.get("/")
async def health():
    return {"status": "ok", "upstream": CTYUN_BASE_URL}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
