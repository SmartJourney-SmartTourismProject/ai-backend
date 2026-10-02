# app/config/settings.py
"""
Centralized configuration management for SmartJourney AI Backend.
Loads all settings from environment variables with sensible defaults.

Every external service configured here has a free tier with no card
required (project decision, docs/master_plan/API_SETUP.md). Verify all of
them at once with `python scripts/check_apis.py`.
"""
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    # ===== LLM & AI Configuration =====
    # Field name matches app/utils/slot_filling.py's `settings.gemini_api_key`.
    # Defaults to "" (not required) so importing this module without a .env
    # doesn't raise ValidationError - e.g. in CI or on a teammate's fresh clone.
    gemini_api_key: str = ""
    llm_temperature: float = 0.0
    llm_top_p: float = 1.0
    llm_top_k: int = 1
    llm_timeout_s: float = 20.0

    # Provider failover chain (decision D6b): "<provider>:<model>", comma
    # separated, tried in order on 429/503/timeout. Providers with no key
    # configured are skipped automatically - see app/core/llm.py.
    llm_provider_chain: str = (
        "gemini:gemini-3.5-flash-lite,gemini:gemini-3.6-flash,groq:openai/gpt-oss-120b"
    )
    groq_api_key: str = ""
    # Optional paid providers, selectable in Admin > AI models
    # (app/core/llm_config.py). Keys entered in the admin panel override these.
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    # AES-256-GCM key (32 bytes, base64) that decrypts API keys an admin saved
    # in the DB (llm_provider_key). Must match backend/.env's value.
    settings_encryption_key: str = ""
    # Shared secret NestJS sends as X-Internal-Token to /internal/llm/*.
    # Empty = those routes are disabled.
    internal_api_token: str = ""
    # Comma-separated browser origins allowed by CORS. NestJS calls this
    # service server-to-server (no CORS involved), so this only matters for
    # direct browser access such as Swagger or a local frontend.
    cors_allowed_origins: str = "http://localhost:3000,http://localhost:3001,http://localhost:8000"
    # How often the LLM config is re-read from the DB (an admin save also
    # triggers an immediate reload via /internal/llm/reload).
    llm_config_refresh_s: float = 30.0

    # Purposes to try in Groq-first order (app/core/llm.py's get_llm()) -
    # empty by default as of the itinerary-quality/token-reduction pass.
    # History: RecommendationOutput/PlannerOutput's schema (nested object
    # arrays, several array max_length bounds, UUID/date/time pattern
    # constraints) was live-confirmed 2026-09-03 to reliably fail against
    # BOTH configured Gemini models with a bare, non-quota 400
    # INVALID_ARGUMENT (reproduced across two different Gemini
    # accounts/keys, ruling out a key/quota explanation). That forced these
    # two purposes onto Groq first - but Groq's free tier is 8,000 TPM
    # against Gemini's ~1,000,000, and a single recommend call alone
    # measured ~5,000 tokens, so this traded a 400 for a 413 almost every
    # time (plan_source: "llm" was observed exactly once, ever - see
    # TODO.md). The actual fix was the schema itself:
    # app/models/schemas.py dropped the pattern constraints and array
    # max_length bounds that upstream reports (e.g.
    # https://github.com/vercel/ai/issues/21192) describe as exactly this
    # failure shape - L1's validate_referential and L2's
    # geo_in_country/dates_in_window already enforce the same things more
    # strongly, without costing a provider-side schema-translation
    # attempt. Live-reverified against gemini-3.5-flash-lite post-fix
    # (scripts/check_llm_chain_reliability.py): clean structured-output
    # success, no 400. Gemini's ~125x larger TPM budget is what actually
    # makes the LLM path usable, so it stays first for every purpose now.
    llm_provider_chain_groq_first_purposes: str = ""

    # ===== RAG / knowledge base (app/rag/) =====
    # Off by default: a fresh clone/CI has no embedded chunks yet, and every
    # retrieve.py caller already degrades to "no answer" rather than
    # raising when this is False - see app/rag/retrieve.py.
    enable_rag: bool = False

    # Embedding provider chain - same "<provider>:<model>" shape as
    # llm_provider_chain (app/core/llm.py), read by app/rag/embeddings.py.
    # gemini:gemini-embedding-001 first: free tier, and GEMINI_API_KEY is
    # already configured for the chat LLM above.
    #
    # Team decision 2026-09-30: may switch to a paid OpenAI embedding model
    # later, not yet certain. That switch is meant to be this one line -
    # EMBEDDING_PROVIDER_CHAIN=openai:text-embedding-3-small - plus
    # OPENAI_API_KEY and `pip install openai`; embeddings.py's OpenAI branch
    # is already written, just lazy-imports the `openai` package so it
    # isn't a hard dependency until then. Switching providers/dimensions
    # requires a full re-embed (knowledge_chunk.embedding_model records
    # which model produced each vector) - old and new can't be compared.
    embedding_provider_chain: str = "gemini:gemini-embedding-001"
    # Matryoshka-truncated (Gemini's embedding model supports this natively;
    # OpenAI's text-embedding-3-* models do too) - smaller than the 3072
    # default, which keeps the HNSW index and the (small) corpus cheap
    # without a meaningful quality loss for this corpus size.
    embedding_dim: int = 768
    openai_api_key: str = ""

    rag_top_k: int = 5
    # Cosine similarity floor below which retrieve.py reports "nothing
    # relevant" rather than handing the LLM weak passages to paraphrase
    # into a confident-sounding wrong answer.
    rag_min_score: float = 0.55

    # ===== Database Configuration =====
    # The single DB setting - points at the same PostgreSQL (PostGIS) instance
    # the NestJS backend owns. See backend/docs/BACKEND_PLAN.md §2 for which
    # service owns which table. Left blank, every DB-backed lookup fails
    # loudly (DataUnavailable) rather than falling back to mock data -
    # see docs/master_plan/DATA_PLATFORM.md §9.
    database_url: str = ""

    # ===== External API Keys =====
    # All free tier, no card required - docs/master_plan/API_SETUP.md.
    openweather_api_key: str = ""
    ticketmaster_api_key: str = ""
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # Real road travel times (decision, DATA_PLATFORM.md §7). Matrix V2 is
    # 500 req/day on the free tier - one many-to-many call per district per
    # plan, never per-pair. api.openrouteservice.org is deprecated and
    # throttled since Aug 2026; use api.heigit.org instead.
    ors_api_key: str = ""
    ors_base_url: str = "https://api.heigit.org/openrouteservice"

    # booking-com15 (the original provider) is gone from RapidAPI as of
    # 2026-09; pick a description-lineage match and set both key and host -
    # see API_SETUP.md §4.2. Real nightly hotel prices when configured;
    # falls back to cost_reference otherwise.
    booking_rapidapi_key: str = ""
    booking_rapidapi_host: str = "booking-com15.p.rapidapi.com"

    # ===== Server Configuration =====
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False
    log_level: str = "INFO"

    # ===== Security Configuration =====
    secret_key: str = "your-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # ===== Error Tracking =====
    sentry_dsn: Optional[str] = None

    # ===== Feature Flags =====
    enable_weather_alerts: bool = True
    enable_disaster_alerts: bool = True
    enable_calendar_integration: bool = True
    enable_policy_guardrails: bool = True

    # ===== Cache Configuration =====
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl: int = 3600

    # ===== Currency (decision D14) =====
    # Base currency is LKR everywhere. This rate is for display conversion
    # only and for converting Booking.com's USD prices at ingest time.
    usd_lkr_rate: float = 310.0

    # ===== ReAct bounds (AGENT_ARCHITECTURE.md §2.1) =====
    # The single place to change how many ReAct turns an agent gets before
    # it's forced to answer with whatever it's observed so far
    # (app/core/react.py's ReActConfig.max_steps reads this by default) -
    # change here, or set REACT_MAX_STEPS in .env, and every agent picks it
    # up without touching app/agents/*.py. Capped at 3 (2026-09-02): each
    # extra step is a real LLM call, and 3 was judged enough turns for the
    # tool catalogs involved (no agent needs more than 6 tools) without
    # letting a confused run rack up cost/latency chasing a bad path.
    react_max_steps: int = 3
    # Same single-knob pattern, for total tool executions per agent run
    # (a cached repeat of an identical call is free, see react.py). Every
    # agent used to hardcode its own value here (12/10/10/6) - centralized
    # 2026-09-02 so tuning this is one number, not four call sites.
    react_tool_budget: int = 12

    # ===== Repair loop (AGENT_ARCHITECTURE.md §3.5) =====
    # A second repair attempt used to be impossible outright
    # (_route_after_verify only ever allowed repair_attempts == 0 through) -
    # raised to 2 (2026-09-26, user decision) since a stochastic failure
    # (as opposed to a systematic one - see repair_temperature_step's own
    # comment) has a real, independent chance of passing on a second try.
    # _route_after_verify's own no-progress guard is what keeps this from
    # just paying for two identical failures when the cause IS systematic.
    max_repair_attempts: int = 2
    # Each repair attempt raises the model's temperature by this much over
    # settings.llm_temperature (attempt 1: +0.1, attempt 2: +0.2 for the
    # default max_repair_attempts=2) - a repair re-sends the EXACT same
    # prompt/candidates as the failed attempt before it, so at temperature
    # 0 it has a real chance of reproducing the identical wrong answer
    # verbatim. A small, escalating nudge gives each retry an actual chance
    # of landing somewhere different, without the plan-quality cost of a
    # high temperature on the original (unrepaired) call, which stays at
    # llm_temperature throughout.
    repair_temperature_step: float = 0.1

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Create global settings instance
settings = Settings()
