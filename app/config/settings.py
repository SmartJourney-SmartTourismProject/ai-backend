# app/config/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict
## this use to import API Keys to python so we dont have to hardcode them in the code.

class Settings(BaseSettings):
    gemini_api_key: str
    openweather_api_key: str = ""   # not needed yet, but fine to have here already
    database_url: str = ""          # will matter once Supabase is wired in — leave blank for now

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()