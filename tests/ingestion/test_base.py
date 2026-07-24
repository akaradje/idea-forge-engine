"""Tests for idea_forge.ingestion.base: RawDocument validation + unique_key.

Covers spec acceptance criteria 1-3 (specs/2026-07-24-reddit-ingestion-adapter.md §5).
"""

from datetime import UTC, datetime

import pytest
from idea_forge.ingestion.base import IngestionAdapter, RawDocument
from pydantic import ValidationError


def _valid_kwargs(**overrides: object) -> dict:
    base = dict(
        source="reddit",
        source_id="t3_abc",
        title="Some problem I have",
        body="details here",
        url="https://www.reddit.com/r/foo/comments/abc",
        author="someuser",
        created_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
        metadata={"subreddit": "foo"},
    )
    base.update(overrides)
    return base


# --- Criterion 1: unique_key ------------------------------------------------


def test_unique_key_is_source_colon_source_id():
    doc = RawDocument(**_valid_kwargs(source="reddit", source_id="t3_abc"))
    assert doc.unique_key == "reddit:t3_abc"


def test_unique_key_differs_for_different_source_id():
    doc_a = RawDocument(**_valid_kwargs(source_id="t3_abc"))
    doc_b = RawDocument(**_valid_kwargs(source_id="t3_xyz"))
    assert doc_a.unique_key != doc_b.unique_key


def test_unique_key_differs_for_different_source():
    doc_a = RawDocument(**_valid_kwargs(source="reddit", source_id="t3_abc"))
    doc_b = RawDocument(**_valid_kwargs(source="arxiv", source_id="t3_abc"))
    assert doc_a.unique_key != doc_b.unique_key


# --- Criterion 2: required fields --------------------------------------------


@pytest.mark.parametrize("missing_field", ["title", "url", "created_at"])
def test_missing_required_field_raises_validation_error(missing_field):
    kwargs = _valid_kwargs()
    del kwargs[missing_field]
    with pytest.raises(ValidationError):
        RawDocument(**kwargs)


def test_missing_fetched_at_raises_validation_error():
    kwargs = _valid_kwargs()
    del kwargs["fetched_at"]
    with pytest.raises(ValidationError):
        RawDocument(**kwargs)


def test_missing_source_or_source_id_raises_validation_error():
    for field in ("source", "source_id"):
        kwargs = _valid_kwargs()
        del kwargs[field]
        with pytest.raises(ValidationError):
            RawDocument(**kwargs)


# --- Criterion 3: optional fields default -----------------------------------


def test_missing_body_defaults_to_empty_string():
    kwargs = _valid_kwargs()
    del kwargs["body"]
    doc = RawDocument(**kwargs)
    assert doc.body == ""


def test_missing_author_defaults_to_none():
    kwargs = _valid_kwargs()
    del kwargs["author"]
    doc = RawDocument(**kwargs)
    assert doc.author is None


def test_missing_metadata_defaults_to_empty_dict():
    kwargs = _valid_kwargs()
    del kwargs["metadata"]
    doc = RawDocument(**kwargs)
    assert doc.metadata == {}


def test_metadata_default_is_not_shared_between_instances():
    # Guards against a mutable-default bug (Field(default_factory=dict) is correct;
    # a plain `= {}` default would be shared across instances).
    kwargs_a = _valid_kwargs()
    del kwargs_a["metadata"]
    kwargs_b = _valid_kwargs()
    del kwargs_b["metadata"]
    doc_a = RawDocument(**kwargs_a)
    doc_b = RawDocument(**kwargs_b)
    doc_a.metadata["mutated"] = True
    assert doc_b.metadata == {}


# --- IngestionAdapter contract shape (supports criteria covered in test_reddit.py) --


def test_ingestion_adapter_is_abstract_and_cannot_be_instantiated():
    with pytest.raises(TypeError):
        IngestionAdapter()  # type: ignore[abstract]


def test_ingestion_adapter_requires_fetch_implementation():
    class Incomplete(IngestionAdapter):
        source = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_ingestion_adapter_subclass_with_fetch_is_instantiable():
    class Dummy(IngestionAdapter):
        source = "dummy"

        async def fetch(self):
            if False:
                yield  # pragma: no cover - makes this an async generator

    Dummy()
