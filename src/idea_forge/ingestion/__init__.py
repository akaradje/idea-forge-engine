"""Ingestion layer: pluggable adapters that pull raw documents from external sources."""

from idea_forge.ingestion.base import IngestionAdapter, RawDocument

__all__ = ["IngestionAdapter", "RawDocument"]
