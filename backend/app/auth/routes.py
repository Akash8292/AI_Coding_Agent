from flask import Blueprint, request, jsonify, g
from app.database import db
from app.models.user import User
from app.auth.utils import generate_token, require_auth
import re

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@auth_bp.route("/register", methods=["POST"])
def register():
    """POST /auth/register — create a new user account."""
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    name = (body.get("name") or "").strip()

    if not email or not EMAIL_RE.match(email):
        return jsonify({"error": "A valid email address is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with this email already exists"}), 409

    user = User(email=email, name=name or email.split("@")[0])
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    token = generate_token(user.id)
    return jsonify({"token": token, "user": user.to_dict()}), 201


@auth_bp.route("/login", methods=["POST"])
def login():
    """POST /auth/login — authenticate and return JWT."""
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Invalid email or password"}), 401

    token = generate_token(user.id)
    return jsonify({"token": token, "user": user.to_dict()})


@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    """GET /auth/me — return current user info."""
    return jsonify({"user": g.current_user.to_dict()})


@auth_bp.route("/me", methods=["PUT"])
@require_auth
def update_me():
    """PUT /auth/me — update name or password."""
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user

    if "name" in body:
        user.name = (body["name"] or "").strip()
    if "password" in body:
        new_pw = body["password"] or ""
        if len(new_pw) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400
        user.set_password(new_pw)
    if "settings" in body and isinstance(body["settings"], dict):
        user.settings = {**(user.settings or {}), **body["settings"]}

    db.session.commit()
    return jsonify({"user": user.to_dict()})
