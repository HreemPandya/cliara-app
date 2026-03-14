"""
Cliara Cloud — FastAPI proxy entry point.

Exposes an OpenAI-compatible API at /v1/* that:
  - Authenticates callers via Supabase JWTs
  - Enforces per-user monthly rate limits
  - Forwards LLM requests to OpenAI using Cliara's API keys
  - Streams responses back verbatim

Deploy on Railway:
  railway up --service cliara-cloud
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from proxy import router as proxy_router
from settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Do NOT validate settings here — if any required env var is missing,
    # get_settings() raises and the app never starts, so /health never
    # responds and Railway's healthcheck fails. Validate on first use
    # (proxy or /v1/usage) so the deploy succeeds and the user gets a
    # clear error when they hit the API without configuring.
    yield


app = FastAPI(
    title="Cliara Cloud API",
    description="OpenAI-compatible proxy with per-user auth and rate limiting.",
    version="0.1.0",
    lifespan=lifespan,
    # Disable the interactive docs in production (no API key leakage risk)
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to cliara.dev in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(proxy_router, prefix="/v1")


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request, exc: RuntimeError):
    """Return 503 when settings are missing (e.g. env vars not set in Railway)."""
    msg = str(exc).strip()
    if "Missing required environment" in msg or "SUPABASE" in msg.upper():
        return JSONResponse(
            status_code=503,
            content={
                "error": "not_configured",
                "message": msg,
                "hint": "Set SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET, OPENAI_API_KEY in Railway variables.",
            },
        )
    raise exc


@app.get("/health")
async def health():
    """Railway health-check endpoint."""
    return {"status": "ok", "service": "cliara-cloud"}


@app.get("/v1/usage")
async def usage_info():
    """
    Public endpoint returning tier information.
    Detailed per-user usage is available after authentication.
    """
    try:
        settings = get_settings()
    except RuntimeError as e:
        return JSONResponse(
            status_code=503,
            content={
                "error": "not_configured",
                "message": str(e).strip(),
                "hint": "Set SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET, OPENAI_API_KEY in Railway variables.",
            },
        )
    return {
        "free_tier": {
            "queries_per_month": settings.free_tier_limit,
            "model": settings.free_tier_model,
            "price": "$0",
        },
        "upgrade_url": "https://cliara.dev/pricing",
    }
