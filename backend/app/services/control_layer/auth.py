"""
Step Authentication - Phase 12.3

Provides token generation and validation for step-to-backend communication.
Tokens are JWT-based and include step ID for authorization.
"""
import jwt
import time
from datetime import datetime, timedelta
from typing import Optional

# Secret key for signing tokens - in production, load from config
# For now, use a placeholder that can be overridden
_SECRET_KEY = "lazyaf-step-auth-secret-key-change-in-production"
_ALGORITHM = "HS256"

# Default token expiration (24 hours)
DEFAULT_EXPIRATION_SECONDS = 86400


def generate_step_token(
    step_id: str,
    execution_key: str,
    expires_in_seconds: int = DEFAULT_EXPIRATION_SECONDS,
) -> str:
    """
    Generate a JWT token for step authentication.

    Args:
        step_id: ID of the step execution
        execution_key: Unique execution key
        expires_in_seconds: Token expiration time

    Returns:
        JWT token string
    """
    now = datetime.utcnow()
    payload = {
        "step_id": step_id,
        "execution_key": execution_key,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in_seconds),
    }

    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def validate_step_token(
    token: str,
    step_id: str,
) -> bool:
    """
    Validate a step authentication token.

    Args:
        token: JWT token to validate
        step_id: Expected step ID in the token

    Returns:
        True if token is valid and matches step_id, False otherwise
    """
    try:
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])

        # Check step_id matches
        if payload.get("step_id") != step_id:
            return False

        # Token is valid and not expired (jwt.decode checks expiration)
        return True

    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False


def decode_step_token(token: str) -> Optional[dict]:
    """
    Decode a step token without validating step_id.

    Args:
        token: JWT token to decode

    Returns:
        Token payload dict or None if invalid
    """
    try:
        return jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
    except jwt.InvalidTokenError:
        return None


def set_secret_key(key: str) -> None:
    """
    Set the secret key for token signing.
    Should be called during app initialization.

    Args:
        key: Secret key string
    """
    global _SECRET_KEY
    _SECRET_KEY = key
