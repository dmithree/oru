"""Environment config for self-reflection agent."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    tasks_hub_url: str = "http://oru-tasks-hub:8004"

    state_file: str = "/opt/state/self-reflection.json"
    processed_log: str = "/opt/state/self-reflection-processed.json"
    personal_context_file: str = "/opt/state/personal-context.json"

    therapy_dir: str = "/opt/data/personal/therapy/transcripts/summary"
    coach_dir: str = "/opt/data/personal/coach/transcripts/summary"

    # Window of recent summaries to consider on each run.
    since_days: int = 120
    max_files_per_run: int = 8

    analyze_cron: str = "0 19 * * sun"

    http_host: str = "0.0.0.0"
    http_port: int = 8005


settings = Settings()
