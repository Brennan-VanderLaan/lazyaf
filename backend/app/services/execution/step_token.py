"""
Step Token Service (Phase 12.3).

Generates and validates one-time tokens for step execution authentication.
Each step gets a unique token that allows it to communicate with the backend.

Token Flow:
1. LocalExecutor generates token before spawning container
2. Token is written to /workspace/.control/step_config.json
3. Control layer reads token and uses it in Authorization header
4. Backend validates token on each request

Security Properties:
- Tokens are cryptographically random (32 bytes, URL-safe base64)
- One token per step execution
- Tokens can be revoked
- Tokens expire after TTL (default 24 hours)
"""
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials


# In-memory token store
# In production, this would use Redis or database for persistence
_tokens: dict[str, dict] = {}

# Security scheme for FastAPI dependency injection
security = HTTPBearer(auto_error=False)


def generate_step_token(step_id: str, ttl_hours: int = 24) -> str:
    """
    Generate a one-time token for step execution.

    Args:
        step_id: The step execution key (e.g., "run-123:0:1")
        ttl_hours: Token time-to-live in hours

    Returns:
        Token string to be included in step_config.json
    """
    # Generate cryptographically secure random token
    token = secrets.token_urlsafe(32)

    # Store hash of token (we never store the raw token)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    _tokens[token_hash] = {
        "step_id": step_id,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(hours=ttl_hours),
    }

    return token


def validate_step_token(token: str) -> Optional[dict]:
    """
    Validate a step token.

    Args:
        token: The raw token string

    Returns:
        Token data dict if valid, None if invalid
    """
    if not token:
        return None

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    if token_hash not in _tokens:
        return None

    token_data = _tokens[token_hash]

    # Check expiration
    if token_data["expires_at"] < datetime.utcnow():
        # Clean up expired token
        del _tokens[token_hash]
        return None

    return token_data


def revoke_step_token(step_id: str) -> int:
    """
    Revoke all tokens for a step.

    Args:
        step_id: The step execution key

    Returns:
        Number of tokens revoked
    """
    to_remove = [
        h for h, data in _tokens.items()
        if data["step_id"] == step_id
    ]

    for token_hash in to_remove:
        del _tokens[token_hash]

    return len(to_remove)


def cleanup_expired_tokens() -> int:
    """
    Remove all expired tokens from the store.

    Returns:
        Number of tokens cleaned up
    """
    now = datetime.utcnow()
    to_remove = [
        h for h, data in _tokens.items()
        if data["expires_at"] < now
    ]

    for token_hash in to_remove:
        del _tokens[token_hash]

    return len(to_remove)


# -----------------------------------------------------------------------------
# FastAPI Dependency for Token Verification
# -----------------------------------------------------------------------------

async def verify_step_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> dict:
    """
    FastAPI dependency to verify step token from Authorization header.

    Usage:
        @router.post("/api/steps/{step_id}/status")
        async def update_status(
            step_id: str,
            token_data: dict = Depends(verify_step_token),
        ):
            # token_data contains {"step_id": "...", ...}

    Raises:
        HTTPException 401 if token is missing or invalid
    """
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )

    token = credentials.credentials
    token_data = validate_step_token(token)

    if token_data is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )

    return token_data


async def verify_step_token_matches(
    step_id: str,
    token_data: dict = Security(verify_step_token),
) -> dict:
    """
    Verify that the token matches the step_id in the URL.

    This prevents using a valid token for step A to access step B.

    Usage:
        @router.post("/api/steps/{step_id}/status")
        async def update_status(
            step_id: str,
            token_data: dict = Depends(verify_step_token_matches),
        ):
            # Guaranteed token matches step_id

    Raises:
        HTTPException 403 if token doesn't match step_id
    """
    if token_data["step_id"] != step_id:
        raise HTTPException(
            status_code=403,
            detail="Token does not match step_id",
        )

    return token_data


# -----------------------------------------------------------------------------
# Test Helpers
# -----------------------------------------------------------------------------

def clear_all_tokens() -> None:
    """Clear all tokens. For testing only."""
    _tokens.clear()


def get_token_count() -> int:
    """Get number of active tokens. For testing/debugging."""
    return len(_tokens)
