import time
from collections import defaultdict
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

from config import JWT_EXPIRES_HOURS, JWT_SECRET
from datetime import datetime, timedelta, timezone

# --- In-memory brute-force protection ---
# Tracks failed login attempts per IP: {ip: [timestamp, ...]}
_login_attempts: dict[str, list[float]] = defaultdict(list)
_WINDOW = 300   # 5 minute sliding window
_MAX_ATTEMPTS = 30  # max failures per window


def check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    attempts = _login_attempts[ip]
    # Remove old attempts outside the window
    _login_attempts[ip] = [t for t in attempts if now - t < _WINDOW]
    if len(_login_attempts[ip]) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again in 5 minutes.",
            headers={"Retry-After": "300"},
        )


def record_failed_login(ip: str) -> None:
    _login_attempts[ip].append(time.monotonic())


def clear_failed_logins(ip: str) -> None:
    _login_attempts.pop(ip, None)


# --- Password helpers ---

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# --- JWT helpers ---

def create_token(user_id: int, is_admin: bool) -> str:
    payload = {
        "sub": str(user_id),
        "admin": is_admin,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRES_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ")
    payload = decode_token(token)
    return {"id": int(payload["sub"]), "is_admin": payload.get("admin", False)}


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
