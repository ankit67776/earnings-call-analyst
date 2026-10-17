from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    # Hosted Postgres + pgvector (Neon). Set in .env.
    database_url: str = ""
    # Optional until Phase 5 (live mode).
    redis_url: str = ""

    # Pipelines call the LLM through one provider-agnostic client, so models can be
    # swapped and compared in evals. Groq (free tier) is the default; Claude is optional.
    llm_provider: str = "groq"
    groq_api_key: str = ""
    anthropic_api_key: str = ""

    # Both support strict JSON-schema structured outputs on Groq.
    analysis_model: str = "openai/gpt-oss-120b"
    live_model: str = "openai/gpt-oss-20b"

    # On-disk response cache and daily usage counters, so dev re-runs cost no quota.
    llm_cache_dir: str = ".cache/llm"
    llm_cache_enabled: bool = True

    deepgram_api_key: str = ""
    voyage_api_key: str = ""

    # SEC EDGAR requires a descriptive User-Agent with a contact email.
    sec_user_agent: str = ""
    # Downloaded source documents (gitignored).
    raw_data_dir: str = "../data/raw"


settings = Settings()
