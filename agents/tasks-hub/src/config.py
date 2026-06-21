"""Environment config for tasks-hub agent."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    linear_api_key: str = ""

    db_file: str = "/opt/state/tasks.db"
    events_file: str = "/opt/state/task-events.jsonl"
    personal_context_file: str = "/opt/state/personal-context.json"

    http_host: str = "0.0.0.0"
    http_port: int = 8004


settings = Settings()
