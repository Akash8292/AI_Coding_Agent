from flask import Blueprint, jsonify, g, request
from app.auth.utils import require_auth
from app.database import db
from app.models.usage import UsageRecord
from sqlalchemy import func
from datetime import datetime, timezone, timedelta

usage_bp = Blueprint("usage", __name__, url_prefix="/api/usage")


@usage_bp.route("", methods=["GET"])
@require_auth
def get_usage():
    """GET /api/usage — per-user usage summary."""
    user = g.current_user
    period = request.args.get("period", "today")  # today, week, month, all

    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)

    records = UsageRecord.query.filter(
        UsageRecord.user_id == user.id,
        UsageRecord.created_at >= start,
    ).all()

    total_input = sum(r.input_tokens for r in records)
    total_output = sum(r.output_tokens for r in records)
    total_cost = sum(r.cost_usd for r in records)

    # Per-provider breakdown
    by_provider = {}
    for r in records:
        if r.provider not in by_provider:
            by_provider[r.provider] = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        by_provider[r.provider]["requests"] += 1
        by_provider[r.provider]["input_tokens"] += r.input_tokens
        by_provider[r.provider]["output_tokens"] += r.output_tokens
        by_provider[r.provider]["cost_usd"] += r.cost_usd

    return jsonify({
        "period": period,
        "requests": len(records),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "cost_usd": round(total_cost, 4),
        "by_provider": by_provider,
    })
