from dataclasses import fields, replace
from pathlib import Path

import pytest

from crypto_event_harvester.normalization import (
    SCHEMA_VERSION,
    UnifiedEvent,
    normalize_event,
    normalize_events,
)
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_conferences
from crypto_event_harvester.providers.icodrops import parse_listing as parse_token_events

FIXTURES = Path(__file__).parent / "fixtures"


def _token_events(section="upcoming"):
    return parse_token_events((FIXTURES / f"icodrops_{section}.html").read_text(), section=section)


def _conference_events():
    return parse_conferences((FIXTURES / "cryptoevents_home.html").read_text())


def _coindar_events():
    return parse_coindar((FIXTURES / "coindar_listing_sample.html").read_text())


def test_shared_schema_has_identical_columns_across_sources():
    token = normalize_event(_token_events()[0])
    conference = normalize_event(_conference_events()[0])
    coindar = normalize_event(_coindar_events()[0])
    assert set(token.to_dict()) == set(conference.to_dict()) == set(coindar.to_dict())
    assert set(token.to_dict()) == {field.name for field in fields(UnifiedEvent)}
    assert token.schema_version == conference.schema_version == SCHEMA_VERSION
    assert token.provider == "icodrops"
    assert conference.provider == "cryptoevents"
    assert coindar.provider == "coindar"
    assert token.event_type == "token_event"
    assert conference.event_type == "conference"
    assert coindar.event_type == "crypto_event"


def test_token_event_provenance_and_unknown_date_are_preserved():
    event = normalize_event(_token_events()[0])
    assert event.event_id == "icodrops:clix:tge-and-distribution"
    assert event.source_event_id == "clix:tge-and-distribution"
    assert event.source_url == "https://icodrops.com/clix/"
    assert event.project == "CLIX"
    assert event.ticker == "CLIX"
    assert event.subtype == "TGE and Distribution"
    assert event.status == "upcoming"
    assert event.date_text == "Upcoming"
    assert event.start_date is None
    assert event.end_date is None
    assert event.date_precision == "unknown"
    assert event.timezone is None
    assert event.start_at_utc is None
    assert event.end_at_utc is None
    assert event.location is None
    assert event.featured is None


def test_icodrops_explicit_day_has_equal_start_and_end():
    event = normalize_event(_token_events("ended")[0])
    assert event.start_date == event.end_date == "2026-09-29"
    assert event.date_precision == "day"
    assert event.date_role == "listed"
    assert event.status == "ended"


def test_cryptoevents_multi_day_range_and_fields():
    event = normalize_event(_conference_events()[0])
    assert event.event_id == "cryptoevents:merge-madrid-2026"
    assert event.source_event_id == "merge-madrid-2026"
    assert event.title == "Merge Madrid 2026"
    assert event.start_date == "2026-10-27"
    assert event.end_date == "2026-10-29"
    assert event.date_precision == "range"
    assert event.date_role == "start"
    assert event.category == "conference"
    assert event.status == "listed"
    assert event.location == "Madrid"
    assert event.description == "MERGE Madrid 2026: institutional crypto and Web3."
    assert event.ticker is None
    assert event.featured is False
    assert event.timezone is None


def test_unscheduled_conference_does_not_gain_imaginary_dates():
    event = normalize_event(_conference_events()[-1])
    assert event.date_text == "Coming soon"
    assert event.date_precision == "unknown"
    assert event.start_date is None and event.end_date is None


def test_normalize_mixed_provider_list_keeps_order_and_distinct_ids():
    originals = [_token_events()[0], _conference_events()[0], _coindar_events()[0],
                 _token_events("ended")[0]]
    records = normalize_events(originals)
    assert [item.provider for item in records] == [
        "icodrops", "cryptoevents", "coindar", "icodrops"
    ]
    assert len({item.event_id for item in records}) == 4


def test_quarter_precision_preserves_raw_label():
    original = _token_events()[0]
    event = normalize_event(
        replace(original, date_text="from Q1, 2027", event_date=None, date_precision="quarter",
                date_role="start")
    )
    assert event.date_precision == "quarter"
    assert event.date_text == "from Q1, 2027"
    assert event.start_date is None and event.end_date is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"date_precision": "day"}, "explicit calendar dates"),
        ({"start_date": "2026-10-27", "end_date": None}, "start_date/end_date"),
        ({"start_date": "2026-10-27", "end_date": "2026-10-28"}, "explicit calendar dates"),
        ({"date_precision": "future"}, "invalid date_precision"),
        ({"date_role": "estimated"}, "invalid date_role"),
        ({"timezone": "UTC"}, "no verified timezone"),
        ({"start_at_utc": "2026-10-27T00:00:00Z"}, "never UTC timestamps"),
        ({"event_id": "other:event"}, "namespaced"),
        ({"title": ""}, "title"),
    ],
)
def test_unknown_date_record_validation(changes, message):
    event = normalize_event(_token_events()[0])
    with pytest.raises(ValueError, match=message):
        replace(event, **changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"start_date": "2026-02-30"}, "invalid ISO"),
        ({"start_date": "2026-10-30", "end_date": "2026-10-29"}, "end_date cannot"),
        ({"start_date": "2026-10-27", "end_date": "2026-10-28"}, "day-precision"),
    ],
)
def test_known_date_validation(changes, message):
    event = normalize_event(_token_events("ended")[0])
    with pytest.raises(ValueError, match=message):
        replace(event, **changes)


def test_mixed_records_duplicate_identity_is_rejected():
    original = _token_events()[0]
    with pytest.raises(ValueError, match="duplicate normalized event_id"):
        normalize_events([original, original])


def test_unknown_source_event_type_is_rejected():
    with pytest.raises(TypeError, match="unsupported provider"):
        normalize_event(object())


def test_coindar_coin_category_and_listing_day_are_normalized():
    event = normalize_event(_coindar_events()[0])
    assert event.event_id == "coindar:155001"
    assert event.source_event_id == "155001"
    assert event.source_url == "https://www.coindar.org/en/event/rotten-candy-hunt-155001"
    assert event.event_type == "crypto_event"
    assert event.title == "Rotten Candy Hunt"
    assert event.project == "Alien Worlds"
    assert event.ticker == "TLM"
    assert event.category == "Contest"
    assert event.start_date == event.end_date == "2026-11-01"
    assert event.date_precision == "day"
    assert event.date_role == "listed"
    assert event.description == "Alien Worlds' Rotten Candy Hunt will conclude on November 1st."
    assert event.timezone is None
    assert event.start_at_utc is None and event.end_at_utc is None


def test_coindar_unknown_quarter_heading_stays_unknown():
    event = normalize_event(_coindar_events()[-1])
    assert event.event_id == "coindar:155004"
    assert event.date_text == "Q1 2027"
    assert event.date_precision == "unknown"
    assert event.start_date is None and event.end_date is None
