from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    # Hosted Postgres + pgvector (Neon). Set in .env.
    database_url: str = ""
    # Run the job worker inside the API process (handy locally; use `python -m app.jobs
    # work` as a separate process instead when deploying).
    worker_in_api: bool = True
    # Not used yet: live sessions fan out inside the API process. Needed only if the API
    # runs as several processes.
    redis_url: str = ""

    # Pipelines call the LLM through one provider-agnostic client, so models can be
    # swapped and compared in evals. Groq (free tier) is the default; Claude is optional.
    llm_provider: str = "groq"
    groq_api_key: str = ""
    anthropic_api_key: str = ""

    # Both support strict JSON-schema structured outputs on Groq.
    analysis_model: str = "openai/gpt-oss-120b"
    live_model: str = "openai/gpt-oss-20b"
    # Answers questions over filings and calls, citing a quote for every sentence.
    chat_model: str = "openai/gpt-oss-120b"
    # Contradiction finder. Claims are pulled from calls by the live model, which matched
    # the analysis model on the extraction eval and is what live mode will run on; each
    # claim is then judged against the 10-K by the verifier.
    call_claims_model: str = "openai/gpt-oss-20b"
    verifier_model: str = "openai/gpt-oss-120b"

    # On-disk response cache and daily usage counters, so dev re-runs cost no quota.
    llm_cache_dir: str = ".cache/llm"
    llm_cache_enabled: bool = True

    # Batch speech-to-text: Whisper on Groq (free tier). Responses are cached on disk.
    stt_provider: str = "groq"
    stt_model: str = "whisper-large-v3"
    stt_cache_dir: str = ".cache/stt"

    # Live speech-to-text: Deepgram's streaming API. Each run is recorded on disk and
    # replayed when the same audio is streamed again with the same settings.
    live_stt_provider: str = "deepgram"
    live_stt_model: str = "nova-3"
    live_stt_cache_dir: str = ".cache/live_stt"
    # One JSON-lines log per live session: every event with the time it was published.
    live_log_dir: str = ".cache/live_sessions"
    deepgram_api_key: str = ""

    # Embeddings go through one client too: "provider/model", where the provider is
    # voyage or gemini. The retrieval eval compares them; vectors from different models
    # are stored side by side and never mixed in one search.
    embedding_model: str = "voyage/voyage-4"
    voyage_api_key: str = ""
    gemini_api_key: str = ""
    embedding_cache_dir: str = ".cache/embeddings"

    # SEC EDGAR requires a descriptive User-Agent with a contact email.
    sec_user_agent: str = ""
    # Downloaded source documents (gitignored).
    raw_data_dir: str = "../data/raw"


settings = Settings()
