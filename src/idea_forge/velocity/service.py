"""Orchestrates HN fetch -> aggregate -> persist snapshot -> build VelocityReport."""

from datetime import UTC, datetime, timedelta

from idea_forge.config import Settings
from idea_forge.ingestion.base import RawDocument
from idea_forge.ingestion.hackernews import HackerNewsAdapter
from idea_forge.velocity.metrics import aggregate, compute_delta, compute_fetch_limit_hit
from idea_forge.velocity.models import EvidencePost, VelocityReport, Watch, WatchListResult
from idea_forge.velocity.store import WatchStore

_NOTE = "Evidence only — no opinion score. Judge acceleration yourself."


def _to_evidence(doc: RawDocument) -> EvidencePost:
    return EvidencePost(
        unique_key=doc.unique_key,
        title=doc.title,
        url=doc.url,
        points=doc.metadata.get("points"),
        num_comments=doc.metadata.get("num_comments"),
        created_at=doc.created_at,
    )


class VelocityService:
    def __init__(self, settings: Settings, store: WatchStore) -> None:
        self.settings = settings
        self.store = store

    async def watch_gap(self, name: str, query: str, tags: list[str]) -> Watch:
        watch = Watch(
            name=name,
            query=query,
            tags=tags,
            created_at=datetime.now(UTC),
            last_checked_at=None,
        )
        return await self.store.add_watch(watch)

    async def unwatch_gap(self, name: str) -> None:
        await self.store.remove_watch(name)

    async def list_watches(self) -> WatchListResult:
        watches = await self.store.list_watches()
        return WatchListResult(watches=watches, count=len(watches))

    async def check_velocity(self, name: str) -> VelocityReport:
        watch = await self.store.get_watch(name)

        previous = await self.store.latest_snapshot(name)

        captured_at = datetime.now(UTC)
        window_start = captured_at - timedelta(days=self.settings.velocity_window_days)

        # Fetch per tag (not all tags combined into one adapter call): the
        # adapter enforces hn_limit_per_tag per tag, so combining tags first
        # and checking len(docs) >= limit against the combined total would
        # false-positive fetch_limit_hit for a multi-tag watch whose tags are
        # each individually well under the cap. See spec §4 "fetch_limit_hit".
        deduped_docs: dict[str, RawDocument] = {}
        any_fetch_limit_hit = False
        for tag in watch.tags:
            hn_settings = self.settings.model_copy(
                update={
                    "hn_query": watch.query,
                    "hn_tags": [tag],
                    "hn_limit_per_tag": self.settings.velocity_limit_per_check,
                }
            )
            adapter = HackerNewsAdapter(hn_settings)
            tag_docs: list[RawDocument] = []
            async with adapter:
                async for doc in adapter.fetch():
                    tag_docs.append(doc)

            if compute_fetch_limit_hit(
                tag_docs,
                fetch_limit=self.settings.velocity_limit_per_check,
                window_start=window_start,
            ):
                any_fetch_limit_hit = True

            for doc in tag_docs:
                # Dedup within the snapshot: the same post can appear under
                # multiple tags and must only be counted/evidenced once.
                deduped_docs.setdefault(doc.unique_key, doc)

        docs = list(deduped_docs.values())

        current = aggregate(
            docs,
            watch_name=name,
            captured_at=captured_at,
            window_days=self.settings.velocity_window_days,
            fetch_limit_hit=any_fetch_limit_hit,
        )
        await self.store.insert_snapshot(current)
        await self.store.touch_last_checked(name, captured_at)

        window_start = current.window_start
        in_window = [doc for doc in docs if doc.created_at >= window_start]

        if previous is not None:
            new_docs = [doc for doc in in_window if doc.created_at > previous.captured_at]
        else:
            new_docs = in_window

        new_docs.sort(key=lambda doc: doc.created_at, reverse=True)
        new_evidence = [
            _to_evidence(doc) for doc in new_docs[: self.settings.velocity_max_evidence]
        ]

        delta = compute_delta(current, previous) if previous is not None else None

        return VelocityReport(
            watch=watch,
            current=current,
            previous=previous,
            delta=delta,
            new_evidence=new_evidence,
            note=_NOTE,
        )
