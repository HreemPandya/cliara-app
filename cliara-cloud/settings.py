"""
Backend configuration — all values come from environment variables.
Set these in Railway's environment variable panel (or a local .env for dev).
"""

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Supabase project
    supabase_url: str
    supabase_service_key: str   # Never expose this — backend only
    supabase_jwt_secret: str    # From Supabase > Settings > API > JWT Secret

    # LLM provider keys — Cliara's own, never sent to clients
    openai_api_key: str
    groq_api_key: str = ""

    # Free tier limits
    free_tier_model: str = "gpt-4o-mini"
    free_tier_limit: int = 150          # queries per calendar month

    # CORS origins allowed to call this API (add cliara.dev in production)
    cors_origins: list = field(default_factory=lambda: ["*"])


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        missing = [
            v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_JWT_SECRET", "OPENAI_API_KEY")
            if not os.getenv(v)
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Set them in Railway's environment panel or a local .env file."
            )
        _settings = Settings(
            supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
            supabase_service_key=os.environ["SUPABASE_SERVICE_KEY"],
            supabase_jwt_secret=os.environ["SUPABASE_JWT_SECRET"],
            openai_api_key=os.environ["OPENAI_API_KEY"],
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            free_tier_model=os.getenv("FREE_TIER_MODEL", "gpt-4o-mini"),
            free_tier_limit=int(os.getenv("FREE_TIER_LIMIT", "150")),
        )
    return _settings
