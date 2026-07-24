"""Gap Detection layer (pipeline stage 3)."""

from idea_forge.gaps.detector import GapDetector
from idea_forge.gaps.errors import GapDetectionError
from idea_forge.gaps.models import GapAnalysis, GapCandidate, GapEvidence

__all__ = [
    "GapAnalysis",
    "GapCandidate",
    "GapDetectionError",
    "GapDetector",
    "GapEvidence",
]
