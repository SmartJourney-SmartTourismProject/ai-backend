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
    # Verified 2026-09-02: real model id, free-tier eligible (plan decision D6).
    llm_model: str = "gemini-3.5-flash-lite"
    llm_temperature: float = 0.0
    llm_top_p: float = 1.0
    llm_top_k: int = 1
    llm_max_tokens: int = 2048
    llm_timeout_s: float = 20.0

    # Provider failover chain (decision D6b): "<provider>:<model>", comma
    # separated, tried in order on 429/503/timeout. Providers with no key
    # configured are skipped automatically - see app/core/llm.py.
    llm_provider_chain: str = (
        "gemini:gemini-3.5-flash-lite,gemini:gemini-3.6-flash,groq:openai/gpt-oss-120b"
    )
    # Optional per-agent override for the orchestrator only (used if a
    # stronger model is needed for tool-calling quality - see AGENT_ARCHITECTURE.md §3.2).
    llm_model_orchestrator: str = ""
    groq_api_key: str = ""

    # Response narration (LLM #5) is off by default - the template is free
    # and deterministic (decision D6c). Turn on only if the generated prose
    # is worth the extra call against the daily quota.
    enable_response_narration: bool = False

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

    # Lazy, budget-capped enrichment only (500 free Pro calls/month since
    # June 2026 - too few for bulk sync). See DATA_PLATFORM.md §5.1/§4.1.
    foursquare_api_key: str = ""
    foursquare_monthly_budget: int = 400

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

    # ===== RAG & Vector Store Configuration =====
    # Demoted, not deleted (decision D11): off the request path, since it
    # was re-ranking a list the deterministic scorer already ranks better
    # and reproducibly. Kept for a future knowledge-document Q&A feature.
    # Needs requirements-rag.txt installed to enable.
    embeddings_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    enable_rag: bool = False

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

    # ===== API Rate Limiting =====
    # Not enforced yet (SAD §10.3) - out of scope this round (decision D15).
    rate_limit_per_minute: int = 60
    rate_limit_per_day: int = 1000

    # ===== Currency (decision D14) =====
    # Base currency is LKR everywhere. This rate is for display conversion
    # only and for converting Booking.com's USD prices at ingest time.
    usd_lkr_rate: float = 310.0

    # ===== ReAct bounds (AGENT_ARCHITECTURE.md §2.1) =====
    react_max_steps: int = 6
    react_tool_budget: int = 12

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Create global settings instance
settings = Settings()
