"""Revision-aware point-in-time facts and snapshots."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeAlias

from .fingerprint import fingerprint
from .time import ensure_aware, to_utc

Revision: TypeAlias = int | str


class PITConflictError(RuntimeError):
    """The newest visible fact is ambiguous rather than uniquely selectable."""


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value


def revision_key(value: Revision) -> tuple[int, int | str]:
    """Return the frozen mixed-type ordering used in Python and SQLite.

    Integer revisions sort above string revisions; within each kind normal
    numeric/lexical ordering applies. ``bool`` is rejected because it is an
    ``int`` subclass but is never a valid revision identifier.
    """

    if isinstance(value, bool):
        raise TypeError("boolean is not a valid revision")
    if isinstance(value, int):
        return (1, value)
    if isinstance(value, str) and value:
        return (0, value)
    raise TypeError("revision must be a non-empty string or an integer")


def encode_revision(value: Revision) -> tuple[str, str]:
    """Encode a revision without erasing its original type."""

    kind, normalized = revision_key(value)
    return ("INTEGER", str(normalized)) if kind == 1 else ("STRING", str(normalized))


def decode_revision(kind: str, value: str) -> Revision:
    """Reverse :func:`encode_revision` with strict validation."""

    if kind == "INTEGER":
        try:
            decoded = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid INTEGER revision: {value!r}") from exc
        if str(decoded) != str(value):
            raise ValueError(f"non-canonical INTEGER revision: {value!r}")
        return decoded
    if kind == "STRING" and isinstance(value, str) and value:
        return value
    raise ValueError(f"invalid revision encoding: {kind!r}, {value!r}")


@dataclass(frozen=True, slots=True)
class PITFact:
    """One append-only fact with explicit historical availability."""

    namespace: str
    entity_id: str
    field: str
    event_time: datetime
    known_at: datetime
    usable_from: datetime
    revision: Revision
    payload: Any
    source: str
    verified: bool = True

    def __post_init__(self) -> None:
        for name in ("namespace", "entity_id", "field", "source"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        _require_bool(self.verified, "verified")
        ensure_aware(self.event_time, "event_time")
        ensure_aware(self.known_at, "known_at")
        ensure_aware(self.usable_from, "usable_from")
        if to_utc(self.usable_from) < to_utc(self.known_at):
            raise ValueError("usable_from cannot precede known_at")
        revision_key(self.revision)
        fingerprint(self.payload)

    @property
    def identity(self) -> tuple[str, str, str, datetime]:
        return (
            self.namespace,
            self.entity_id,
            self.field,
            to_utc(self.event_time),
        )

    @property
    def fact_id(self) -> str:
        return fingerprint(self)


def select_latest(facts: Iterable[PITFact]) -> PITFact:
    """Select the newest visible revision, failing on an unresolved tie."""

    candidates = tuple(facts)
    if not candidates:
        raise LookupError("no point-in-time fact candidates")
    newest_known_at = max(to_utc(fact.known_at) for fact in candidates)
    newest = [fact for fact in candidates if to_utc(fact.known_at) == newest_known_at]
    highest_revision = max(revision_key(fact.revision) for fact in newest)
    finalists = [fact for fact in newest if revision_key(fact.revision) == highest_revision]
    payload_ids = {fingerprint(fact.payload) for fact in finalists}
    if len(payload_ids) != 1:
        raise PITConflictError(
            "latest facts share known_at/revision but disagree on payload"
        )
    sources = {fact.source for fact in finalists}
    if len(sources) != 1:
        raise PITConflictError(
            "latest facts share known_at/revision but disagree on source"
        )
    return min(finalists, key=lambda fact: fact.fact_id)


@dataclass(frozen=True, slots=True)
class PITSnapshot:
    as_of: datetime
    facts: tuple[PITFact, ...]
    snapshot_id: str

    def get(self, namespace: str, entity_id: str, field: str) -> PITFact | None:
        matches = (
            fact
            for fact in self.facts
            if (fact.namespace, fact.entity_id, fact.field)
            == (
                namespace,
                entity_id,
                field,
            )
        )
        return max(
            matches,
            key=lambda fact: to_utc(fact.event_time),
            default=None,
        )


class PointInTimeStore:
    """In-memory append-only store used by adapters and deterministic tests."""

    def __init__(self, facts: Iterable[PITFact] = ()) -> None:
        self._facts: list[PITFact] = []
        self._fact_ids: set[str] = set()
        self.extend(facts)

    def add(self, fact: PITFact) -> None:
        if not isinstance(fact, PITFact):
            raise TypeError("PointInTimeStore only accepts PITFact")
        if fact.fact_id in self._fact_ids:
            return
        self._facts.append(fact)
        self._fact_ids.add(fact.fact_id)

    def extend(self, facts: Iterable[PITFact]) -> None:
        for fact in facts:
            self.add(fact)

    @property
    def facts(self) -> tuple[PITFact, ...]:
        return tuple(self._facts)

    def snapshot(
        self,
        as_of: datetime,
        *,
        require_verified: bool = True,
    ) -> PITSnapshot:
        _require_bool(require_verified, "require_verified")
        cutoff = to_utc(as_of, "as_of")
        grouped: dict[tuple[str, str, str, datetime], list[PITFact]] = defaultdict(list)
        for fact in self._facts:
            if require_verified and not fact.verified:
                continue
            if to_utc(fact.known_at) <= cutoff and to_utc(fact.usable_from) <= cutoff:
                grouped[fact.identity].append(fact)
        selected = tuple(
            sorted(
                (select_latest(group) for group in grouped.values()),
                key=lambda fact: fact.identity,
            )
        )
        snapshot_id = fingerprint(
            {
                "schema": "pit-snapshot-v1",
                "as_of": cutoff,
                "facts": [fact.fact_id for fact in selected],
                "require_verified": require_verified,
            }
        )
        return PITSnapshot(as_of=cutoff, facts=selected, snapshot_id=snapshot_id)
