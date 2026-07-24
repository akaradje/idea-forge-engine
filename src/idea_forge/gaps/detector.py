"""Gap Detection layer (pipeline stage 3): turns ingested documents into
structured demand-supply gap candidates via the Anthropic API's structured
output.

Retry policy: no hand-rolled retry loop here. The Anthropic SDK's built-in
retries (default max_retries=2) handle 429/5xx/connection errors with
backoff for its own error taxonomy — this is the deliberate exception to
the project's tenacity-based retry rule.
"""

import logging

from anthropic import APIError, AsyncAnthropic
from pydantic import ValidationError

from idea_forge.config import Settings
from idea_forge.gaps.errors import GapDetectionError
from idea_forge.gaps.models import GapAnalysis, GapCandidate
from idea_forge.ingestion.base import RawDocument

logger = logging.getLogger(__name__)

SYSTEM_PROMPT: str = """You are a demand-supply gap analyst for an innovation research pipeline.

You will be given a batch of documents (Reddit posts, Hacker News posts, or similar
demand-signal sources), each with a unique_key, title, body, and optional metadata
(points, num_comments).

Your task: identify concrete, unmet-need "gaps" — problems where demand signals look
strong (people complaining, asking for solutions, expressing frustration, upvoting/
commenting heavily) but existing supply (known products, tools, or solutions) looks weak
or absent.

For each gap you identify, produce:
- problem: a concise statement of the unmet need.
- evidence: a list of {unique_key, quote} pairs. Every evidence quote MUST cite the
  unique_key of the document it came from, and the quote must be a short verbatim
  snippet from that document's title or body. Do not fabricate quotes or unique_keys.
- demand_signal: a short explanation of why demand looks high (e.g. repeated
  complaints, high engagement, explicit requests for a solution).
- supply_signal: a short explanation of existing solutions found in the documents or
  general knowledge. If no existing solution is evident anywhere in the batch, you
  MUST set this field to the literal string "none observed" — do not leave it blank
  or write a different phrase for "nothing found".
- confidence: a float between 0.0 and 1.0 reflecting how confident you are that this
  is a real, well-evidenced gap.
- metadata: optional additional structured notes (may be empty).

If the batch contains no clear gaps, return an empty gaps list rather than inventing
weak or speculative ones. Every gap must be traceable to at least one document via
its unique_key.
"""

BODY_TRUNCATE_CHARS = 2000  # per-doc body budget in the prompt

_METADATA_ALLOWLIST_KEYS = ("points", "num_comments")


class GapDetector:
    """Analyzes ingested RawDocuments for demand-supply gaps via Claude.

    See module docstring for retry policy.
    """

    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        self._settings = settings
        if client is not None:
            self._client = client
            return

        if not settings.anthropic_api_key and not _env_has_api_key():
            raise GapDetectionError(
                "Anthropic API key not configured: set anthropic_api_key in Settings "
                "or the ANTHROPIC_API_KEY environment variable."
            )

        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key or None,
            timeout=settings.gap_request_timeout_seconds,
        )

    async def detect(self, docs: list[RawDocument]) -> list[GapCandidate]:
        """Batch docs by gap_max_docs_per_call, analyze each batch sequentially
        (avoids burst/rate pressure in v1), and concatenate results in batch
        order. No cross-batch dedup (v2 / caller concern)."""
        if not docs:
            return []

        batches = self._batch(docs, self._settings.gap_max_docs_per_call)
        results: list[GapCandidate] = []
        for batch in batches:
            results.extend(await self._detect_batch(batch))

        logger.info(
            "gap detection complete: total_docs=%d batch_count=%d total_gaps=%d",
            len(docs),
            len(batches),
            len(results),
        )
        return results

    async def _detect_batch(self, docs: list[RawDocument]) -> list[GapCandidate]:
        """One Anthropic structured call; returns that batch's gaps (possibly empty).

        Uses `messages.create` with an explicit `output_config` json_schema (rather
        than `messages.parse`) so we control the order of checks ourselves:
        `messages.parse` validates eagerly inside the SDK call, which would turn a
        real refusal (natural-language text block) or a max_tokens truncation into
        a run-aborting ValidationError before `stop_reason` can be inspected.
        """
        prompt = self._serialize_docs(docs)
        try:
            response = await self._client.messages.create(
                model=self._settings.gap_model,
                max_tokens=self._settings.gap_max_output_tokens,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": GapAnalysis.model_json_schema()}
                },
            )
        except APIError as exc:
            logger.exception(
                "gap detection batch call failed: layer=gaps batch_doc_count=%d",
                len(docs),
            )
            raise GapDetectionError("Anthropic API call failed during gap detection") from exc

        if response.stop_reason == "refusal":
            logger.warning(
                "gap detection batch refused: layer=gaps batch_doc_count=%d",
                len(docs),
            )
            return []

        if response.stop_reason == "max_tokens":
            raise GapDetectionError(
                "gap analysis output truncated at gap_max_output_tokens; lower "
                "gap_max_docs_per_call or raise GAP_MAX_OUTPUT_TOKENS"
            )

        text = next(
            (block.text for block in response.content if block.type == "text"),
            None,
        )
        if text is None:
            raise GapDetectionError("gap analysis response contained no text content block")

        try:
            analysis = GapAnalysis.model_validate_json(text)
        except ValidationError as exc:
            raise GapDetectionError("Gap analysis response failed schema validation") from exc

        return analysis.gaps

    @staticmethod
    def _batch(docs: list[RawDocument], size: int) -> list[list[RawDocument]]:
        return [docs[i : i + size] for i in range(0, len(docs), size)]

    @staticmethod
    def _serialize_docs(docs: list[RawDocument]) -> str:
        """Compact per-doc block: unique_key, title, body truncated to
        BODY_TRUNCATE_CHARS, allow-listed metadata (points, num_comments) when
        present. Input order preserved (deterministic, testable)."""
        blocks: list[str] = []
        for doc in docs:
            body = doc.body[:BODY_TRUNCATE_CHARS]
            lines = [
                f"unique_key: {doc.unique_key}",
                f"title: {doc.title}",
                f"body: {body}",
            ]
            for key in _METADATA_ALLOWLIST_KEYS:
                if key in doc.metadata:
                    lines.append(f"{key}: {doc.metadata[key]}")
            blocks.append("\n".join(lines))
        return "\n\n---\n\n".join(blocks)


def _env_has_api_key() -> bool:
    import os

    return bool(os.environ.get("ANTHROPIC_API_KEY"))
