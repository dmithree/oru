"""Environment config for daily-briefing agent."""
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

    linear_api_key: str = ""

    morning_cron: str = "30 7 * * *"
    evening_cron: str = "45 18 * * *"
    tasks_file: str = "/opt/data/all-tasks.md"
    state_file: str = "/opt/state/daily-briefing.json"
    personal_context_file: str = "/opt/state/personal-context.json"

    http_host: str = "0.0.0.0"
    http_port: int = 8002


settings = Settings()
