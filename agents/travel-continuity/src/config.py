"""Environment config for travel-continuity agent."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    oura_access_token: str = ""
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_redirect_uri: str = "http://localhost"
    strava_refresh_token: str = ""

    memory_dir: str = "/opt/data/memory"
    travel_dir: str = "/opt/data/travel"
    state_file: str = "/opt/state/travel-continuity.json"
    personal_context_file: str = "/opt/state/personal-context.json"

    # Posmotri (Vercel-deployed share service) — public API, no auth needed.
    posmotri_base_url: str = "https://posmotri-eight.vercel.app"

    phase_check_cron: str = "*/30 * * * *"
    pretrip_daily_cron: str = "0 19 * * *"

    home_timezone: str = "Europe/Belgrade"
    home_latitude: float = 44.7866
    home_longitude: float = 20.4489

    http_host: str = "0.0.0.0"
    http_port: int = 8003


settings = Settings()
