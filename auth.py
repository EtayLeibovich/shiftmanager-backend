import os
import logging
from datetime import datetime, timedelta
from jose import JWTError, jwt

# SECURITY: SECRET_KEY must be set as an environment variable in production.
# Never hardcode secrets in source code.
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    logging.warning(
        "⚠️  WARNING: SECRET_KEY env var is not set! "
        "Using insecure default key. Set SECRET_KEY to a long random string in production!"
    )
    SECRET_KEY = "insecure-dev-key-MUST-CHANGE-IN-PRODUCTION-xk9p3m2024"

ALGORITHM = "HS256"
EXPIRE_HOURS = 24       # Full access tokens expire after 24 hours
PENDING_EXPIRE_MINUTES = 5  # 2FA pending tokens expire after 5 minutes


def create_access_token(data: dict) -> str:
    """Create a signed JWT access token with a 24-hour expiration."""
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=EXPIRE_HOURS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_pending_token(data: dict) -> str:
    """
    Create a short-lived JWT used as a 2FA challenge token.
    The payload must include a 'scope' field ('2fa_pending' or '2fa_setup')
    so that this token is explicitly rejected by endpoints that require a full token.
    Expires in 5 minutes.
    """
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(minutes=PENDING_EXPIRE_MINUTES)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict | None:
    """
    Decode and verify a JWT token.
    Returns the payload dict, or None if the token is invalid or expired.
    """
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
