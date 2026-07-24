"""Reusable ingestion adapter contract shared by all data-source adapters."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RawDocument(BaseModel):
    source: str
    source_id: str
    title: str
    body: str = ""
    url: str
    author: str | None = None
    created_at: datetime
    fetched_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def unique_key(self) -> str:
        return f"{self.source}:{self.source_id}"


class IngestionAdapter(ABC):
    source: str

    @abstractmethod
    def fetch(self) -> AsyncIterator[RawDocument]:
        """Async generator; yields normalized documents lazily.

        Chosen over `-> list[...]` so pagination streams without buffering
        all pages.
        """
        ...
