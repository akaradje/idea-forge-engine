"""Tests for idea_forge.gaps.models: GapEvidence, GapCandidate, GapAnalysis.

Covers spec acceptance criteria 1-4 (specs/2026-07-24-gap-detection.md §5).
"""

import pytest
from idea_forge.gaps import GapAnalysis, GapCandidate, GapEvidence
from pydantic import ValidationError

# --- Criterion 1: GapCandidate minimal-valid + defaults ----------------------


def test_gap_candidate_validates_with_required_fields_and_defaults():
    candidate = GapCandidate(
        problem="p",
        demand_signal="d",
        supply_signal="none observed",
        confidence=0.5,
    )

    assert candidate.problem == "p"
    assert candidate.demand_signal == "d"
    assert candidate.supply_signal == "none observed"
    assert candidate.confidence == 0.5
    assert candidate.evidence == []
    assert candidate.metadata == {}


# --- Criterion 2: confidence bounds -------------------------------------------


def test_gap_candidate_confidence_above_one_raises_validation_error():
    with pytest.raises(ValidationError):
        GapCandidate(problem="p", demand_signal="d", supply_signal="s", confidence=1.5)


def test_gap_candidate_confidence_below_zero_raises_validation_error():
    with pytest.raises(ValidationError):
        GapCandidate(problem="p", demand_signal="d", supply_signal="s", confidence=-0.1)


# --- Criterion 3: GapEvidence --------------------------------------------------


def test_gap_evidence_validates_with_unique_key_and_quote():
    evidence = GapEvidence(unique_key="reddit:abc", quote="some quote")

    assert evidence.unique_key == "reddit:abc"
    assert evidence.quote == "some quote"


def test_gap_evidence_missing_unique_key_raises_validation_error():
    with pytest.raises(ValidationError):
        GapEvidence(quote="some quote")


# --- Criterion 4: GapAnalysis ---------------------------------------------------


def test_gap_analysis_model_validate_empty_gaps_list():
    analysis = GapAnalysis.model_validate({"gaps": []})

    assert analysis.gaps == []


def test_gap_analysis_defaults_gaps_to_empty_list():
    analysis = GapAnalysis()

    assert analysis.gaps == []


def test_gap_analysis_holds_gap_candidates():
    candidate = GapCandidate(problem="p", demand_signal="d", supply_signal="s", confidence=0.5)
    analysis = GapAnalysis(gaps=[candidate])

    assert analysis.gaps == [candidate]
