from datetime import datetime, timedelta
from jose import JWTError, jwt

SECRET_KEY = "shiftmanager-saas-secret-key-2024-xk9p3m"
ALGORITHM = "HS256"
EXPIRE_HOURS = 24


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=EXPIRE_HOURS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
