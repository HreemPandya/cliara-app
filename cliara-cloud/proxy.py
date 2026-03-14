"""
OpenAI-compatible proxy endpoints.

The client (Cliara) sends requests exactly as it would to OpenAI.
This module:
  1. Strips the caller's Cliara JWT
  2. Enforces the free-tier model
  3. Checks the monthly rate limit
  4. Forwards to OpenAI with Cliara's own API key
  5. Streams the response back verbatim (preserving SSE framing)
"""

import json
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from auth_middleware import get_current_user
from rate_limit import check_and_increment
from settings import get_settings

router = APIRouter()

_OPENAI_BASE = "https://api.openai.com/v1"

# Models that free-tier users are not allowed to request
_PREMIUM_MODELS = {
    "gpt-4o",
    "gpt-4-turbo",
    "claude-3-opus-20240229",
    "claude-3-5-sonnet-20241022",
}


def _openai_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def _stream_upstream(response: httpx.Response) -> AsyncIterator[bytes]:
    """Yield raw bytes from an httpx streaming response."""
    async for chunk in response.aiter_bytes():
        yield chunk


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    user: dict = Depends(get_current_user),
):
    settings = get_settings()
    user_id: str = user["sub"]

    # Rate-limit check + increment (raises 429 if over limit)
    usage = await check_and_increment(user_id)

    body = await request.json()

    # Free tier: always use gpt-4o-mini regardless of requested model
    # (tier column can be added later for paid tiers)
    body["model"] = settings.free_tier_model

    headers = _openai_headers(settings.openai_api_key)
    streaming: bool = body.get("stream", False)

    response_headers = {
        "X-Cliara-Queries-Used": str(usage["queries_used"]),
        "X-Cliara-Queries-Limit": str(usage["limit"]),
        "X-Cliara-Reset-Date": usage["reset_date"],
    }

    try:
        if streaming:
            # Open a persistent streaming connection to OpenAI and pipe
            # chunks straight through to the client without buffering.
            async def generate() -> AsyncIterator[bytes]:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream(
                        "POST",
                        f"{_OPENAI_BASE}/chat/completions",
                        headers=headers,
                        json=body,
                    ) as upstream:
                        if upstream.status_code != 200:
                            error_body = await upstream.aread()
                            # Emit an SSE error event so the client's stream
                            # parser surfaces it cleanly rather than hanging.
                            yield (
                                b'data: {"error": {"message": '
                                + json.dumps(error_body.decode()).encode()
                                + b", \"type\": \"upstream_error\"}}\n\n"
                            )
                            return
                        async for chunk in upstream.aiter_bytes():
                            yield chunk

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers=response_headers,
            )
        else:
            async with httpx.AsyncClient(timeout=120) as client:
                upstream = await client.post(
                    f"{_OPENAI_BASE}/chat/completions",
                    headers=headers,
                    json=body,
                )
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type="application/json",
                headers=response_headers,
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream LLM timed out. Please retry.")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway error: {exc}")


@router.post("/embeddings")
async def embeddings(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Proxy embedding requests to OpenAI.
    Embeddings are not counted against the monthly query limit — they are
    used for semantic history search and are extremely cheap.
    """
    settings = get_settings()
    body = await request.json()

    # Always use the same embedding model regardless of what the client requests
    body["model"] = "text-embedding-3-small"

    headers = _openai_headers(settings.openai_api_key)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            upstream = await client.post(
                f"{_OPENAI_BASE}/embeddings",
                headers=headers,
                json=body,
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type="application/json",
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Embedding request timed out.")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Gateway error: {exc}")
