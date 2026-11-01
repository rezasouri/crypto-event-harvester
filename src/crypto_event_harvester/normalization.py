"""Provider-agnostic, date-safe event schema for the scraping pilot.

Each independent provider is responsible only for source parsing. This module
maps native provider records to a stable, versioned event representation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import TYPE_CHECKING

from .date_precision import coarse_period

if TYPE_CHECKING:
    from .providers.coindar import CoindarEvent
    from .providers.cryptoevents import CryptoEventsEvent
    from .providers.icodrops import ICODropsEvent

SCHEMA_VERSION = "1.1"
LEGACY_SCHEMA_VERSION = "1.0"
VALID_PRECISIONS = frozenset({"day", "range", "month", "quarter", "unknown"})
VALID_DATE_ROLES = frozenset({"listed", "start", "unknown"})


@dataclass(frozen=True, slots=True)
class UnifiedEvent:
    """One logical source event, preserving date precision and provenance.

    A date-only source cannot safely be translated to a UTC instant. The
    timestamp/timezone fields therefore remain None for current providers.
    """

    schema_version: str
    event_id: str
    provider: str
    source_event_id: str
    source_url: str
    listing_url: str
    event_type: str
    title: str
    project: str | None
    ticker: str | None
    category: str | None
    subtype: str | None
    status: str | None
    location: str | None
    description: str | None
    date_text: str
    start_date: str | None
    end_date: str | None
    date_precision: str
    date_role: str
    timezone: str | None
    start_at_utc: str | None
    end_at_utc: str | None
    total_raised: str | None
    pre_valuation: str | None
    featured: bool | None
    period_key: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
            raise ValueError("unsupported schema_version")
        if not self.provider or not self.source_event_id:
            raise ValueError("missing source event identity")
        if self.event_id != f"{self.provider}:{self.source_event_id}":
            raise ValueError("event_id must be namespaced by provider")
        if not self.title.strip() or not self.source_url or not self.listing_url:
            raise ValueError("event title and source URLs are mandatory")
        if self.date_precision not in VALID_PRECISIONS:
            raise ValueError(f"invalid date_precision: {self.date_precision}")
        if self.schema_version == LEGACY_SCHEMA_VERSION:
            if self.date_precision == "month" or self.period_key is not None:
                raise ValueError("schema 1.0 cannot encode a structured month/period")
        elif self.date_precision in {"month", "quarter"}:
            precision, key = coarse_period(self.date_text)
            if self.date_precision != precision or self.period_key != key:
                raise ValueError("period_key must match an explicit coarse date label")
        elif self.period_key is not None:
            raise ValueError("period_key is only valid for month or quarter precision")
        if self.date_role not in VALID_DATE_ROLES:
            raise ValueError(f"invalid date_role: {self.date_role}")
        known = self.date_precision in {"day", "range"}
        if known != (self.start_date is not None and self.end_date is not None):
            raise ValueError("explicit calendar dates must match precision")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date/end_date must be both present or both absent")
        if known:
            try:
                start = date.fromisoformat(self.start_date or "")
                end = date.fromisoformat(self.end_date or "")
            except ValueError as exc:
                raise ValueError("invalid ISO calendar date") from exc
            if start > end:
                raise ValueError("end_date cannot precede start_date")
            if self.date_precision == "day" and start != end:
                raise ValueError("day-precision event requires equal dates")
        if self.start_at_utc is not None or self.end_at_utc is not None:
            raise ValueError("current providers supply dates only, never UTC timestamps")
        if self.timezone is not None:
            raise ValueError("current providers have no verified timezone information")

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_event(event: ICODropsEvent | CryptoEventsEvent | CoindarEvent) -> UnifiedEvent:
    """Normalize one supported provider event, refusing unknown event types."""
    from .providers.coindar import CoindarEvent
    from .providers.cryptoevents import CryptoEventsEvent
    from .providers.icodrops import ICODropsEvent

    if isinstance(event, ICODropsEvent):
        source_id = event.event_id.removeprefix("icodrops:")
        return UnifiedEvent(
            schema_version=SCHEMA_VERSION,
            event_id=event.event_id,
            provider=event.source,
            source_event_id=source_id,
            source_url=event.source_url,
            listing_url=event.listing_url,
            event_type="token_event",
            title=event.title,
            project=event.project,
            ticker=event.ticker,
            category=event.category,
            subtype=event.round,
            status=event.status,
            location=None,
            description=None,
            date_text=event.date_text,
            start_date=event.event_date,
            end_date=event.event_date,
            date_precision=event.date_precision,
            date_role=event.date_role,
            timezone=None,
            start_at_utc=None,
            end_at_utc=None,
            total_raised=event.total_raised,
            pre_valuation=event.pre_valuation,
            featured=None,
            period_key=coarse_period(event.date_text)[1]
            if event.date_precision == "quarter" else None,
        )
    if isinstance(event, CoindarEvent):
        source_id = event.event_id.removeprefix("coindar:")
        return UnifiedEvent(
            schema_version=SCHEMA_VERSION,
            event_id=event.event_id,
            provider=event.source,
            source_event_id=source_id,
            source_url=event.source_url,
            listing_url=event.listing_url,
            event_type="crypto_event",
            title=event.title,
            project=event.coin,
            ticker=event.ticker,
            category=event.category,
            subtype=None,
            status="listed",
            location=None,
            description=event.description,
            date_text=event.date_text,
            start_date=event.event_date,
            end_date=event.event_date,
            date_precision=event.date_precision,
            date_role=event.date_role,
            timezone=None,
            start_at_utc=None,
            end_at_utc=None,
            total_raised=None,
            pre_valuation=None,
            featured=None,
            period_key=coarse_period(event.date_text)[1]
            if event.date_precision in {"month", "quarter"} else None,
        )
    if isinstance(event, CryptoEventsEvent):
        source_id = event.event_id.removeprefix("cryptoevents:")
        return UnifiedEvent(
            schema_version=SCHEMA_VERSION,
            event_id=event.event_id,
            provider=event.source,
            source_event_id=source_id,
            source_url=event.source_url,
            listing_url=event.listing_url,
            event_type="conference",
            title=event.title,
            project=None,
            ticker=None,
            category=event.category,
            subtype=None,
            status="listed",
            location=event.location,
            description=event.description,
            date_text=event.date_text,
            start_date=event.start_date,
            end_date=event.end_date,
            date_precision=event.date_precision,
            date_role="start" if event.start_date else "unknown",
            timezone=None,
            start_at_utc=None,
            end_at_utc=None,
            total_raised=None,
            pre_valuation=None,
            featured=event.featured,
        )
    raise TypeError(f"unsupported provider event type: {type(event).__name__}")


def normalize_events(events: list[ICODropsEvent | CryptoEventsEvent | CoindarEvent]) -> list[UnifiedEvent]:
    """Preserve input order, making duplicate IDs a validation error."""
    records = [normalize_event(event) for event in events]
    seen: set[str] = set()
    for record in records:
        if record.event_id in seen:
            raise ValueError(f"duplicate normalized event_id: {record.event_id}")
        seen.add(record.event_id)
    return records
