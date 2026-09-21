from app.database import db
from datetime import datetime, timezone

# Token cost per 1M tokens (USD) — approximate as of 2024
COST_PER_MILLION = {
    "openai": {
        "gpt-5.6-terra": {"input": 2.50, "output": 10.00},
        "gpt-5.6-sol": {"input": 3.00, "output": 12.00},
        "gpt-5.6-luna": {"context": 1.00, "input": 1.00, "output": 4.00},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "default": {"input": 2.50, "output": 10.00}
    },
    "anthropic": {
        "claude-sonnet-5": {"input": 3.00, "output": 15.00},
        "claude-opus-4-8": {"input": 15.00, "output": 75.00},
        "claude-3-7-sonnet": {"input": 3.00, "output": 15.00},
        "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
        "default": {"input": 3.00, "output": 15.00}
    },
    "gemini": {
        "gemini-3.6-flash": {"input": 0.075, "output": 0.30},
        "gemini-3.5-flash-lite": {"input": 0.05, "output": 0.20},
        "gemini-3.1-pro-preview": {"input": 1.25, "output": 5.00},
        "gemini-2.0-flash": {"input": 0.075, "output": 0.30},
        "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
        "default": {"input": 0.075, "output": 0.30}
    },
    "openrouter": {"default": {"input": 2.00, "output": 10.00}},
    "kimi": {
        "kimi-k2.7-code": {"input": 0.80, "output": 2.50},
        "default": {"input": 1.00, "output": 3.00}
    },
    "ollama": {"default": {"input": 0.00, "output": 0.00}},
}


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    provider_costs = COST_PER_MILLION.get(provider, {})
    model_costs = provider_costs.get(model, provider_costs.get("default", {"input": 0, "output": 0}))
    return (input_tokens * model_costs["input"] + output_tokens * model_costs["output"]) / 1_000_000


class UsageRecord(db.Model):
    __tablename__ = "usage_records"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=True)
    provider = db.Column(db.String(50), nullable=False)
    model = db.Column(db.String(100), nullable=False)
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cost_usd = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    # Relationship
    user = db.relationship("User", back_populates="usage_records")

    @classmethod
    def record(cls, user_id: int, provider: str, model: str,
               input_tokens: int, output_tokens: int,
               conversation_id: int = None) -> "UsageRecord":
        cost = estimate_cost(provider, model, input_tokens, output_tokens)
        record = cls(
            user_id=user_id,
            conversation_id=conversation_id,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
        db.session.add(record)
        return record

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
