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
    google_calendar_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

'''
Design choices worth flagging:

Silent try/except on the IP call — matches §8's error-handling rule: 
location/calendar failures should fall through to asking the user, never crash the graph. 
If ipapi.co is down or the IP is unresolvable, you just fall to None rather than propagating an exception up through the Orchestrator.

client_gps/client_ip as params, not pulled from state directly —
 keeps this function testable in isolation (call it with fake GPS/IP values without needing a full TripState), 
 and it's the Orchestrator's job to decide what request data flows in, not this tool's.

No API key needed — ipapi.co's free tier works with a plain IP-in-URL request, 
no settings.py entry required for this one.
'''