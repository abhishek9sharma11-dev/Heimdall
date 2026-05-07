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
    meeting_id: str = ""
    meeting_password: str = ""
    meeting_zak: str = ""
    meeting_webinar_token: str = ""  # panelist tk= token from panelist join link

    # Zoom SDK credentials (the bridge reads these too)
    zoom_sdk_key: str = ""
    zoom_sdk_secret: str = ""

    # LLM — OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_model: str = "google/gemma-3-27b-it:free"

    # Identity
    bot_display_name: str = "Om Asnani AI"
    bot_avatar_url: str = ""
    bot_disclosure_line: str = (
        "Hi! I'm Om Asnani's AI co-host — happy to help while he's presenting."
    )

    # Host
    host_email: str = ""

    # Behavior
    greet_new_attendees: bool = True
    greet_delay_seconds: int = 3
    greet_max_per_minute: int = 10
    answer_questions: bool = True
    answer_rate_limit_per_user_per_min: int = 2

    # RAG
    knowledge_dir: Path = Path("./knowledge")
    vector_store_path: Path = Path("./vector_store")
    embedding_model: str = "all-MiniLM-L6-v2"
    rag_top_k: int = 4


settings = Settings()
