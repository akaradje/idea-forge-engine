"""Structured-output schema for gap detection."""

from typing import Any

from pydantic import BaseModel, Field


class GapEvidence(BaseModel):
    unique_key: str  # references RawDocument.unique_key
    quote: str  # short verbatim snippet supporting the gap


class GapCandidate(BaseModel):
    problem: str  # concise unmet-need statement
    evidence: list[GapEvidence] = Field(default_factory=list)
    demand_signal: str  # why demand looks high
    supply_signal: str  # existing solutions found; "none observed" if absent
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GapAnalysis(BaseModel):
    """Structured-output wrapper: the top-level schema the model must return."""

    gaps: list[GapCandidate] = Field(default_factory=list)
