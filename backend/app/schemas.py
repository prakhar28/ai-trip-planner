from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class RunCreate(BaseModel):
    prompt: str = Field(min_length=10, max_length=2000)


class RunOut(BaseModel):
    id: str
    status: str
    prompt: str
    result: dict | None = None
    error: str | None = None
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- The agent's final answer. The model must satisfy this schema. ----------

class Stop(BaseModel):
    time: str | None = Field(default=None, description="Rough slot, e.g. '9:00' or 'Evening'")
    name: str
    description: str = Field(max_length=300)
    estimated_cost: float = Field(ge=0, description="Cost per traveller in the itinerary currency")


class Day(BaseModel):
    day: int = Field(ge=1)
    title: str
    stops: list[Stop] = Field(min_length=1, max_length=8)


class Itinerary(BaseModel):
    destination: str
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="ISO 4217 code, e.g. USD")
    total_budget: float = Field(gt=0)
    estimated_total: float = Field(ge=0)
    budget_breakdown: dict[str, float] = Field(
        description="Category -> amount, e.g. {'lodging': 400, 'food': 250}. Must sum to estimated_total."
    )
    weather_note: str
    days: list[Day] = Field(min_length=1, max_length=14)
    tips: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def check_consistency(self) -> "Itinerary":
        numbers = [d.day for d in self.days]
        if numbers != list(range(1, len(self.days) + 1)):
            raise ValueError(f"days must be numbered 1..{len(self.days)} in order, got {numbers}")

        breakdown_sum = round(sum(self.budget_breakdown.values()), 2)
        if abs(breakdown_sum - self.estimated_total) > max(1.0, self.estimated_total * 0.01):
            raise ValueError(
                f"budget_breakdown sums to {breakdown_sum} but estimated_total is {self.estimated_total}. "
                "Use calculate_budget and copy its numbers."
            )

        if self.estimated_total > self.total_budget:
            raise ValueError(
                f"Plan costs {self.estimated_total} {self.currency}, over the {self.total_budget} budget. "
                "Swap or drop expensive items and resubmit."
            )
        return self
