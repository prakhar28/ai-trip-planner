from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    planner_model: str = "gemini-3.6-flash"

    database_url: str = "postgresql+asyncpg://trip:trip@localhost:5432/trip"
    redis_url: str = "redis://localhost:6379/0"

    # Guardrails: one bad request must never run forever or drain credits.
    max_turns: int = 12
    max_output_tokens: int = 4096
    run_token_budget: int = 150_000
    run_timeout_seconds: int = 300
    tool_timeout_seconds: float = 20.0
    worker_max_jobs: int = 10

    # Cost tracking (USD per million tokens). Set from the official pricing page.
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    # Gemini caches repeated prompt prefixes automatically and bills cached tokens at a
    # discount. Verify this multiplier against the pricing page.
    cache_read_multiplier: float = 0.25

    # Retries for rate limits (429) and server errors (5xx).
    api_max_retries: int = 4

    tool_cache_ttl_seconds: int = 3600
    cors_origins: str = "http://localhost:5173"


settings = Settings()
