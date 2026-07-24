"""Application settings loaded from environment / .env."""

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str

    reddit_subreddits: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SomebodyMakeThis"]
    )
    reddit_listing: str = "new"
    reddit_limit_per_subreddit: int = 100
    reddit_page_size: int = 100
    reddit_top_time_filter: str = "week"

    request_timeout_seconds: float = 10.0
    max_retries: int = 3

    @field_validator("reddit_subreddits", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values sourced from env/.env at runtime
