# app/config/settings.py
"""
Centralized configuration management for SmartJourney AI Backend.
Loads all settings from environment variables with sensible defaults.
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
    llm_model: str = "gemini-3.6-flash"  # gemini-2.0-flash/2.5-flash were retired/404 for new keys
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048

    # ===== Database Configuration =====
    # The single DB setting - points at the same PostgreSQL (PostGIS) instance
    # the NestJS backend owns. See backend/docs/BACKEND_PLAN.md §2 for which
    # service owns which table. Left blank, every DB-backed lookup falls back
    # to mock data / local files, so this service still runs standalone.
    database_url: str = ""

    # ===== External API Keys =====
    openweather_api_key: str = ""
    google_maps_api_key: str = ""
    ticketmaster_api_key: str = ""
    yelp_fusion_api_key: str = ""
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    booking_rapidapi_key: str = ""  # RapidAPI key for Booking.com (real hotel nightly prices)
    booking_rapidapi_host: str = "booking-com15.p.rapidapi.com"

    # ===== Server Configuration =====
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False
    log_level: str = "INFO"

    # ===== RAG & Vector Store Configuration =====
    vector_store_type: str = "faiss"  # faiss or pinecone
    embeddings_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    enable_rag: bool = True

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
    rate_limit_per_minute: int = 60
    rate_limit_per_day: int = 1000

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Create global settings instance
settings = Settings()
