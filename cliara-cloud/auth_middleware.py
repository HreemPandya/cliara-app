"""
JWT verification middleware for the Cliara Cloud proxy.

Supabase can issue JWTs with either:
  - HS256 (legacy): signed with JWT_SECRET
  - ES256/RS256 (new): signed with asymmetric keys; verify via JWKS

We support both so tokens work regardless of Supabase project configuration.
"""

import jwt
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from settings import get_settings

_bearer_scheme = HTTPBearer(auto_error=True)


def verify_supabase_jwt(token: str) -> dict:
    """
    Decode and validate a Supabase JWT.
    Supports HS256 (legacy JWT secret) and ES256/RS256 (JWKS).
    Returns the payload dict on success; raises HTTPException on failure.
    """
    settings = get_settings()
    try:
        unverified = jwt.get_unverified_header(token)
        alg = unverified.get("alg", "HS256")

        if alg == "HS256":
            payload = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
        else:
            # ES256 or RS256 — verify via Supabase JWKS
            jwks_url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
            try:
                from jwt import PyJWKClient
                jwks_client = PyJWKClient(jwks_url)
                signing_key = jwks_client.get_signing_key_from_jwt(token)
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "config_error",
                        "message": "PyJWKClient not available. Install: pip install pyjwt[crypto]",
                    },
                )
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256"],
                audience="authenticated",
            )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "token_expired",
                "message": "Your Cliara session has expired. Run 'cliara login' to refresh.",
            },
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_token",
                "message": f"Invalid token: {exc}. Run 'cliara login' to re-authenticate.",
            },
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """
    FastAPI dependency that extracts and validates the caller's JWT.
    Returns the decoded payload (contains `sub` = Supabase user UUID).
    """
    return verify_supabase_jwt(credentials.credentials)
