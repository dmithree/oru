"""Environment config for health agent. Fails fast on missing secrets."""
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

    digest_cron: str = "0 18 * * SUN"
    state_file: str = "/opt/state/health.json"

    http_host: str = "0.0.0.0"
    http_port: int = 8001


settings = Settings()
