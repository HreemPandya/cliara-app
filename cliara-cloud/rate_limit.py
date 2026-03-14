"""
Per-user monthly rate limiting backed by a Supabase Postgres table.

Table: public.user_usage
  user_id    UUID  (matches Supabase auth.users.id)
  year_month TEXT  "YYYY-MM"
  query_count INT

The backend service key bypasses RLS, so no auth header tricks needed.
"""

from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from settings import get_settings


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _reset_date() -> str:
    """First day of next month in YYYY-MM-DD format."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        return f"{now.year + 1}-01-01"
    return f"{now.year}-{now.month + 1:02d}-01"


def _supabase_headers(service_key: str) -> dict:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def check_and_increment(user_id: str) -> dict:
    """
    Atomically check whether the user is under their monthly limit and
    increment their counter.  Raises HTTP 429 if the limit is reached.

    Returns a dict with usage metadata attached to the response headers.
    """
    settings = get_settings()
    year_month = _current_month()
    headers = _supabase_headers(settings.supabase_service_key)
    base = f"{settings.supabase_url}/rest/v1/user_usage"

    async with httpx.AsyncClient(timeout=10) as client:
        # Fetch current row (if any)
        resp = await client.get(
            base,
            headers=headers,
            params={"user_id": f"eq.{user_id}", "year_month": f"eq.{year_month}"},
        )
        resp.raise_for_status()
        rows = resp.json()
        current = rows[0]["query_count"] if rows else 0

        if current >= settings.free_tier_limit:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "queries_used": current,
                    "limit": settings.free_tier_limit,
                    "reset_date": _reset_date(),
                    "message": (
                        f"Free tier limit reached ({current}/{settings.free_tier_limit} queries this month). "
                        f"Resets on {_reset_date()}. "
                        "Run 'cliara upgrade' to get more queries."
                    ),
                },
            )

        new_count = current + 1
        if rows:
            await client.patch(
                base,
                headers=headers,
                params={"user_id": f"eq.{user_id}", "year_month": f"eq.{year_month}"},
                json={"query_count": new_count},
            )
        else:
            await client.post(
                base,
                headers=headers,
                json={"user_id": user_id, "year_month": year_month, "query_count": 1},
            )

    return {
        "queries_used": new_count,
        "limit": settings.free_tier_limit,
        "reset_date": _reset_date(),
    }
