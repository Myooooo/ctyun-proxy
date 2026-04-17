import asyncio
import json
import logging
import os
import time
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ctyun-proxy")
# Quiet down noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI(title="CTYun Coding Plan Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Configuration ────────────────────────────────────────────────────────────

CTYUN_BASE_URL = os.getenv("CTYUN_BASE_URL", "https://wishub-x6.ctyun.cn/coding")
UPSTREAM_HOST = urlparse(CTYUN_BASE_URL).hostname
API_KEY = os.getenv("API_KEY", "")
DEFAULT_MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "600"))
STREAM_FIRST_CHUNK_TIMEOUT = float(os.getenv("STREAM_FIRST_CHUNK_TIMEOUT", "60"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "GLM-5.1")
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
    headers["host"] = UPSTREAM_HOST
    if API_KEY:
        headers["authorization"] = f"Bearer {API_KEY}"
    return headers


def _is_chat_completions(path: str) -> bool:
    return "chat/completions" in path


def _is_messages(path: str) -> bool:
    segments = path.split("/")
    return segments and segments[-1].split("?")[0] == "messages"


def _override_model(body: bytes) -> bytes:
    """Replace the model field in the request body with DEFAULT_MODEL (if set)."""
    if not DEFAULT_MODEL:
        return body
    try:
        data = json.loads(body)
        if isinstance(data, dict) and "model" in data:
            original = data["model"]
            data["model"] = DEFAULT_MODEL
            if original != DEFAULT_MODEL:
                logger.debug(f"Model overridden: {original} → {DEFAULT_MODEL}")
            return json.dumps(data).encode("utf-8")
    except (json.JSONDecodeError, ValueError):
        pass
    return body


def _parse_stream_flag(body: bytes) -> bool:
    try:
        return json.loads(body).get("stream", False)
    except (json.JSONDecodeError, ValueError):
        return False


def _parse_request_meta(body: bytes) -> dict:
    """Extract stream, message count, max_tokens, model, input_chars from request body."""
    try:
        data = json.loads(body)
        messages = data.get("messages", [])
        input_chars = sum(
            len(m.get("content", "")) if isinstance(m.get("content"), str)
            else sum(len(c.get("text", "")) for c in m.get("content", []) if isinstance(c, dict))
            for m in messages if isinstance(m, dict)
        )
        return {
            "stream": data.get("stream", False),
            "model": data.get("model", "?"),
            "messages": len(messages),
            "input_chars": input_chars,
            "max_tokens": data.get("max_tokens"),
        }
    except (json.JSONDecodeError, ValueError):
        return {}


def _parse_response_usage(body: bytes) -> dict | None:
    """Extract token usage from non-streaming response body."""
    try:
        data = json.loads(body)
        usage = data.get("usage")
        if usage:
            return {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _extract_stream_usage(chunks: list[bytes]) -> dict | None:
    """Extract token usage from collected SSE stream chunks."""
    for chunk in reversed(chunks):
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    data = json.loads(line[6:])
                    usage = data.get("usage")
                    if usage:
                        return {
                            "prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        }
                except (json.JSONDecodeError, ValueError):
                    continue
    return None


def _clean_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def _mask_auth(headers: dict) -> dict:
    """Return headers dict with authorization values masked for safe logging."""
    masked = {}
    for k, v in headers.items():
        if k.lower() == "authorization" and len(v) > 10:
            masked[k] = v[:10] + "..."
        else:
            masked[k] = v
    return masked


async def _backoff(attempt: int):
    await asyncio.sleep(min(2 ** (attempt - 1), 30))


def _should_retry(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _make_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=30.0, read=REQUEST_TIMEOUT, write=60.0, pool=30.0)


def _log_upstream_error(target_url: str, method: str, req_headers: dict, req_body: bytes,
                        status_code: int, resp_body: bytes, attempt: int, max_retries: int):
    """Log upstream error with full request context for debugging."""
    meta = _parse_request_meta(req_body)
    body_preview = req_body[:2000].decode("utf-8", errors="replace")
    resp_text = resp_body[:2000].decode("utf-8", errors="replace")
    resp_usage = _parse_response_usage(resp_body)
    usage_str = f"  Response usage: {resp_usage}\n" if resp_usage else ""
    logger.warning(
        f"Upstream error (attempt {attempt}/{max_retries}): "
        f"{method} {target_url} → {status_code}\n"
        f"  Request meta: {meta}\n"
        f"  Request body: {body_preview}\n"
        f"  Response body: {resp_text}\n"
        f"{usage_str}"
    )


async def _retry_request(target_url: str, headers: dict, body: bytes, method: str, max_retries: int):
    """Retry logic for chat completions and messages endpoints."""
    is_stream = _parse_stream_flag(body)
    last_exc: Exception | None = None
    last_status: int = 0
    last_body: bytes = b""
    last_headers: dict = {}
    start_time = time.time()

    for attempt in range(1, max_retries + 1):
        client = httpx.AsyncClient(timeout=_make_timeout())
        try:
            meta = _parse_request_meta(body)
            input_chars = meta.get("input_chars", 0)
            logger.info(f"↑ attempt {attempt}/{max_retries} model={meta.get('model', '?')} msgs={meta.get('messages', 0)} input_chars={input_chars} body={len(body)} bytes")
            if is_stream:
                # For streaming: open the connection, check status, then stream or retry
                req = client.build_request(method, target_url, headers=headers, content=body)
                resp = await client.send(req, stream=True)

                if _should_retry(resp.status_code) and attempt < max_retries:
                    logger.warning(f"Attempt {attempt}/{max_retries} got {resp.status_code}, retrying...")
                    last_status = resp.status_code
                    last_body = await resp.aread()
                    last_headers = dict(resp.headers)
                    await resp.aclose()
                    await client.aclose()
                    await _backoff(attempt)
                    continue

                if resp.status_code >= 400:
                    content = await resp.aread()
                    _log_upstream_error(target_url, method, headers, body,
                                        resp.status_code, content, attempt, max_retries)
                    await resp.aclose()
                    await client.aclose()
                    return Response(content=content, status_code=resp.status_code, media_type="application/json")

                # Pre-read first chunk to verify stream health before committing response
                stream_iter = resp.aiter_bytes()
                first_chunks: list[bytes] = []
                try:
                    first_chunks.append(
                        await asyncio.wait_for(stream_iter.__anext__(), timeout=STREAM_FIRST_CHUNK_TIMEOUT)
                    )
                except StopAsyncIteration:
                    pass  # empty stream is valid
                except Exception as chunk_exc:
                    logger.warning(f"Stream pre-check failed on attempt {attempt}/{max_retries}: {chunk_exc}")
                    await resp.aclose()
                    await client.aclose()
                    last_exc = chunk_exc
                    if attempt < max_retries:
                        await _backoff(attempt)
                        continue
                    return JSONResponse(status_code=502, content={"error": {"message": str(chunk_exc), "type": "proxy_error"}})

                # Stream verified — commit response
                async def generate(r=resp, c=client, fc=first_chunks, si=stream_iter, st=start_time):
                    collected = list(fc)
                    try:
                        for chunk in fc:
                            yield chunk
                        async for chunk in si:
                            collected.append(chunk)
                            yield chunk
                    except Exception as exc:
                        logger.warning(f"Stream interrupted after {sum(len(c) for c in collected)} bytes: {exc}")
                    finally:
                        total_bytes = sum(len(c) for c in collected)
                        elapsed = time.time() - st
                        usage = _extract_stream_usage(collected)
                        usage_str = (
                            f" input_tokens={usage['prompt_tokens']} output_tokens={usage['completion_tokens']}"
                            if usage else ""
                        )
                        logger.info(f"↓ stream done{usage_str} bytes={total_bytes} time={elapsed:.2f}s")
                        await r.aclose()
                        await c.aclose()

                return StreamingResponse(
                    generate(),
                    status_code=resp.status_code,
                    headers=_clean_headers(dict(resp.headers)),
                )

            else:
                # Non-streaming
                resp = await client.request(method, target_url, headers=headers, content=body)

                if _should_retry(resp.status_code) and attempt < max_retries:
                    logger.warning(f"Attempt {attempt}/{max_retries} got {resp.status_code}, retrying...")
                    last_status = resp.status_code
                    last_body = resp.content
                    last_headers = dict(resp.headers)
                    await client.aclose()
                    await _backoff(attempt)
                    continue

                content = resp.content
                status = resp.status_code
                elapsed = time.time() - start_time
                resp_usage = _parse_response_usage(content)
                usage_str = (
                    f" input_tokens={resp_usage['prompt_tokens']} output_tokens={resp_usage['completion_tokens']}"
                    if resp_usage else ""
                )
                logger.info(f"↓ {status}{usage_str} bytes={len(content)} time={elapsed:.2f}s")
                if resp.status_code >= 400:
                    _log_upstream_error(target_url, method, headers, body,
                                        resp.status_code, content, attempt, max_retries)
                    await client.aclose()
                    return Response(content=content, status_code=resp.status_code, media_type="application/json")
                hdrs = _clean_headers(dict(resp.headers))
                await client.aclose()
                return Response(content=content, status_code=status, headers=hdrs)

        except Exception as exc:
            await client.aclose()
            last_exc = exc
            if attempt < max_retries:
                logger.warning(f"Attempt {attempt}/{max_retries} error: {exc}, retrying...")
                await _backoff(attempt)
                continue

    # All retries exhausted
    if last_status:
        _log_upstream_error(target_url, method, headers, body,
                            last_status, last_body, max_retries, max_retries)
        return Response(content=last_body, status_code=last_status, media_type="application/json")
    logger.error(
        f"All retries exhausted: {method} {target_url}\n"
        f"  Request meta: {_parse_request_meta(body)}\n"
        f"  Request headers: {_mask_auth(headers)}\n"
        f"  Request body: {body[:2000].decode('utf-8', errors='replace')}\n"
        f"  Last exception: {last_exc}"
    )
    return JSONResponse(status_code=502, content={"error": {"message": str(last_exc), "type": "proxy_error"}})


async def _simple_proxy(target_url: str, headers: dict, body: bytes, method: str):
    """Non-retry proxy: on failure return HTTP 200 with error info."""
    try:
        async with httpx.AsyncClient(timeout=_make_timeout()) as client:
            resp = await client.request(method, target_url, headers=headers, content=body)
            return Response(content=resp.content, status_code=resp.status_code, headers=_clean_headers(dict(resp.headers)))
    except Exception as exc:
        logger.error(f"Non-critical request failed: {exc}")
        return JSONResponse(status_code=200, content={"error": {"message": str(exc), "type": "proxy_error"}})


# ── Routes ──────────────────────────────────────────────────────────────────

async def _proxy_request(request: Request, target_url: str, path: str):
    """Common proxy logic for all versioned and root endpoints."""
    headers = _forward_headers(request)
    body = await request.body()

    if _is_chat_completions(path):
        body = _override_model(body)
        meta = _parse_request_meta(body)
        logger.info(f"→ chat/completions model={meta.get('model', '?')} msgs={meta.get('messages', 0)} input_chars={meta.get('input_chars', 0)} (retry={DEFAULT_MAX_RETRIES})")
        return await _retry_request(target_url, headers, body, request.method, DEFAULT_MAX_RETRIES)

    if _is_messages(path):
        body = _override_model(body)
        meta = _parse_request_meta(body)
        logger.info(f"→ messages model={meta.get('model', '?')} msgs={meta.get('messages', 0)} input_chars={meta.get('input_chars', 0)} (retry={DEFAULT_MAX_RETRIES})")
        return await _retry_request(target_url, headers, body, request.method, DEFAULT_MAX_RETRIES)

    logger.info(f"→ {path}")
    return await _simple_proxy(target_url, headers, body, request.method)


def _build_target_url(request: Request, version_prefix: str, path: str) -> str:
    target_url = f"{CTYUN_BASE_URL}/{version_prefix}{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"
    return target_url


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v1(request: Request, path: str):
    return await _proxy_request(request, _build_target_url(request, "v1/", path), path)


@app.api_route("/v2/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v2(request: Request, path: str):
    return await _proxy_request(request, _build_target_url(request, "v2/", path), path)


@app.api_route("/v3/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v3(request: Request, path: str):
    return await _proxy_request(request, _build_target_url(request, "v3/", path), path)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_root(request: Request, path: str):
    if path in ("docs", "openapi.json", "redoc"):
        return

    # Paths already starting with v1/v2/v3 but not matched above (shouldn't happen, fallback)
    target_url = f"{CTYUN_BASE_URL}/v1/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, path)


@app.get("/")
async def health():
    return {"status": "ok", "upstream": CTYUN_BASE_URL}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
