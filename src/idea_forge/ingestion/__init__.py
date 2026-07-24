"""Ingestion layer: pluggable adapters that pull raw documents from external sources."""

from idea_forge.ingestion.base import IngestionAdapter, RawDocument
from idea_forge.ingestion.hackernews import HackerNewsAdapter

__all__ = ["HackerNewsAdapter", "IngestionAdapter", "RawDocument"]
