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

    # Cron schedules (5-field cron syntax: m h dom mon dow). Container TZ
    # is Europe/Belgrade per Dockerfile.
    # Reminders snapshot is refreshed on host every 15 min; we ingest 1
    # minute later to pick up the latest write.
    ingest_reminders_cron: str = "1,16,31,46 * * * *"
    # Markdown + Linear churn slower. Run twice an hour.
    ingest_other_cron: str = "5,35 * * * *"
    # Sunday 21:00 backpressure prompt: list stale tasks, send to Telegram.
    cleanup_cron: str = "0 21 * * sun"
    # Stale threshold for the weekly cleanup (days)
    cleanup_stale_days: int = 30

    # JSON object string mapping Reminders list name -> default context
    # tag. Empty string disables auto-tagging. Example:
    # '{"PFM": "@work", "Tasks": "@work", "Goods": "@shopping"}'
    reminders_list_tags_json: str = ""

    http_host: str = "0.0.0.0"
    http_port: int = 8004


settings = Settings()
