"""
JWT authentication utilities.
"""
import jwt
import datetime
from functools import wraps
from flask import request, jsonify, current_app, g
from app.database import db
from app.models.user import User


def generate_token(user_id: int) -> str:
    """Generate a signed JWT for the given user."""
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "user_id": user_id,
        "iat": now,
        "exp": now + datetime.timedelta(
            hours=current_app.config.get("JWT_EXPIRY_HOURS", 168)
        ),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"],
                      algorithm=current_app.config.get("JWT_ALGORITHM", "HS256"))


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises jwt.InvalidTokenError on failure."""
    return jwt.decode(
        token,
        current_app.config["SECRET_KEY"],
        algorithms=[current_app.config.get("JWT_ALGORITHM", "HS256")],
    )


def require_auth(f):
    """Decorator: requires a valid Bearer token. Sets g.current_user."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Authorization header missing or malformed"}), 401
        token = auth_header[7:]
        try:
            payload = decode_token(token)
            user = db.session.get(User, payload["user_id"])
            if user is None:
                return jsonify({"error": "User not found"}), 401
            g.current_user = user
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired — please log in again"}), 401
        except jwt.InvalidTokenError as e:
            return jsonify({"error": f"Invalid token: {e}"}), 401
        return f(*args, **kwargs)
    return decorated
