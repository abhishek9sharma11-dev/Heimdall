"""Settings loaded from .env. All knobs live here."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Backend
    zoom_backend: Literal["dry_run", "bridge"] = "dry_run"
    bridge_url: str = "http://127.0.0.1:8765"

    # Meeting to join
    meeting_join_url: str = ""  # optional: full Zoom join link (panelist links include tk=)
    meeting_id: str = ""
    meeting_password: str = ""
    meeting_zak: str = ""
    meeting_webinar_token: str = ""  # panelist tk= token from panelist join link

    # Zoom SDK credentials (the bridge reads these too)
    zoom_sdk_key: str = ""
    zoom_sdk_secret: str = ""

    # LLM — OpenAI-compatible endpoint (OpenRouter OR local Ollama)
    # Local example:
    #   OPENROUTER_BASE_URL=http://127.0.0.1:11434/v1
    #   OPENROUTER_API_KEY=ollama
    #   ANTHROPIC_MODEL=qwen2.5:3b
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_model: str = "meta-llama/llama-3.3-70b-instruct:free"

    # Identity — display name MUST contain "AI" or "Assistant"
    bot_display_name: str = "Heimdall AI"
    bot_avatar_url: str = ""
    bot_disclosure_line: str = (
        "Hi! I'm Heimdall AI — the host's co-host, happy to help while they're presenting."
    )

    # Host
    host_email: str = ""

    # Behavior
    greet_new_attendees: bool = True
    greet_delay_seconds: int = 3
    greet_max_per_minute: int = 10
    answer_questions: bool = True
    answer_rate_limit_per_user_per_min: int = 2
    # If false, bridge selects recipient + types reply but does NOT press Enter /
    # click Send — human clicks Submit (safe for live-audience testing).
    chat_auto_submit: bool = True

    # RAG
    knowledge_dir: Path = Path("./knowledge")
    vector_store_path: Path = Path("./vector_store")
    embedding_model: str = "all-MiniLM-L6-v2"
    rag_top_k: int = 4

    # Optional JSON schedule file (session-scoped). Required shape:
    #   {"meeting_id":"<this webinar>","items":[{"time":"16:45:00","text":"..."}, ...]}
    # Won't load if meeting_id != MEETING_ID. Times use SCHEDULE_TZ (default IST).
    schedule_file: Path = Path("")
    schedule_tz: str = "Asia/Kolkata"


settings = Settings()
