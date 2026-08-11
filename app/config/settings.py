# app/config/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict
## this use to import API Keys to python so we dont have to hardcode them in the code.

### Use empty strings if u dont have the key yet so errors doesnt occur at runtime.

class Settings(BaseSettings):
    gemini_api_key: str
    openweather_api_key: str = ""   # not needed yet, but fine to have here already
    database_url: str = ""          # will matter once Supabase is wired in — leave blank for now
    ticketmaster_api_key: str = ""
    eventbrite_api_key: str = ""
    yelp_fusion_api_key: str = ""
    google_calendar_client_id: str = "" 
    google_calendar_client_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()