"""Bounded CryptoEvents HTTP pagination: parsing and failure safeguards."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from crypto_event_harvester import pagination
from crypto_event_harvester.providers.cryptoevents import (
    CryptoEventsError,
    parse_listing,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cryptoevents_home.html"


def _ajax_row(slug="sample-2026", *, first="1794560400", last="1794592800",
              zone="(UTC-05:00) Eastern Time| -5"):
    return {
        "id": "92304",
        "link": f"https://cryptoevents.global/{slug}/",
        "title": "Crypto &#038; Money",
        "excerpt": "<p>Join the conference &amp; summit.</p>",
        "city": "New York, NY",
        "start_date": first,
        "end_date": last,
        "timezone": zone,
    }


def test_parse_real_ajax_response_fields():
    event = pagination.parse_ajax_records([_ajax_row()])[0]
    assert event.event_id == "cryptoevents:sample-2026"
    assert event.title == "Crypto & Money"
    assert event.description == "Join the conference & summit."
    assert event.location == "New York, NY"
    assert event.source_url == "https://cryptoevents.global/sample-2026/"
    assert event.start_date == event.end_date == "2026-11-13"
    assert event.date_precision == "day"
    assert event.featured is False


def test_datetime_uses_explicit_offset_even_if_utc_day_differs():
    # 2026-11-14T01:00Z is still Nov 13 in UTC-05:00.
    epoch = str(int(datetime(2026, 11, 14, 1, tzinfo=UTC).timestamp()))
    item = _ajax_row(first=epoch, last=epoch)
    record = pagination.parse_ajax_records([item])[0]
    assert record.start_date == "2026-11-13"


def test_cross_year_date_range_is_preserved():
    first = str(int(datetime(2026, 12, 31, 18, tzinfo=UTC).timestamp()))
    last = str(int(datetime(2027, 1, 2, 18, tzinfo=UTC).timestamp()))
    item = _ajax_row(first=first, last=last, zone="(UTC+00:00) GMT|0")
    event = pagination.parse_ajax_records([item])[0]
    assert event.start_date == "2026-12-31"
    assert event.end_date == "2027-01-02"
    assert event.date_precision == "range"
    assert event.date_text == "2026-12-31 – 2027-01-02"


def test_missing_timezone_does_not_invent_local_dates():
    record = pagination.parse_ajax_records([_ajax_row(zone="unknown")])[0]
    assert record.start_date is None and record.end_date is None
    assert record.date_precision == "unknown"


@pytest.mark.parametrize("zone", ["UTC", "(UTC+15:00)", "(UTC-05:99)", ""])
def test_invalid_timezone_is_not_treated_as_verified(zone):
    record = pagination.parse_ajax_records([_ajax_row(zone=zone)])[0]
    assert record.start_date is None


def test_reject_foreign_links_and_non_event_items():
    data = [
        _ajax_row(slug="legitimate"),
        {**_ajax_row(slug="attacker"), "link": "https://other.example/attacker/"},
        {**_ajax_row(slug="foo"), "title": ""},
        "unexpected",
        {**_ajax_row(slug="foo"), "link": "javascript:alert(1)"},
    ]
    records = pagination.parse_ajax_records(data)
    assert len(records) == 1
    assert records[0].event_id == "cryptoevents:legitimate"


def test_malformed_ajax_response_is_an_explicit_failure():
    with pytest.raises(CryptoEventsError, match="expected a list"):
        pagination.parse_ajax_records({"error": "unexpected"})


def test_single_page_remains_default_without_any_post(monkeypatch):
    original = parse_listing(FIXTURE.read_text())
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: original)
    monkeypatch.setattr(pagination, "fetch_ajax_page", lambda *a, **kw: pytest.fail("extra request"))
    result = pagination.fetch_paginated_listing()
    assert len(result.events) == 4
    assert result.pages_fetched == 1
    assert result.ajax_requests == 0


def test_page_two_adds_distinct_records_with_one_post(monkeypatch):
    original = parse_listing(FIXTURE.read_text())
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: original)
    offsets = []

    def extra(offset, **kwargs):
        offsets.append((offset, kwargs["start_day_utc"]))
        return (
            pagination.parse_ajax_records([
                _ajax_row(slug=f"new-conference-{i}") for i in range(20)
            ]),
            20,
        )

    monkeypatch.setattr(pagination, "fetch_ajax_page", extra)
    result = pagination.fetch_paginated_listing(max_pages=2, start_day_utc=123456)
    assert len(result.events) == 24
    assert result.pages_fetched == 2
    assert result.ajax_requests == 1
    assert offsets == [(20, 123456)]
    assert result.stop_reason == "page_limit"


def test_early_stop_on_partial_page(monkeypatch):
    original = parse_listing(FIXTURE.read_text())
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: original)
    offsets = []

    def extra(offset, **kw):
        offsets.append(offset)
        return pagination.parse_ajax_records([_ajax_row(slug="new-short-page")]), 1

    monkeypatch.setattr(pagination, "fetch_ajax_page", extra)
    result = pagination.fetch_paginated_listing(max_pages=5, start_day_utc=10)
    assert offsets == [20]
    assert result.stop_reason == "partial_page"
    assert result.pages_fetched == 2


def test_early_stop_on_empty_page(monkeypatch):
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: parse_listing(FIXTURE.read_text()))
    monkeypatch.setattr(pagination, "fetch_ajax_page", lambda *a, **kw: ([], 0))
    result = pagination.fetch_paginated_listing(max_pages=5)
    assert result.stop_reason == "empty_page"
    assert result.pages_fetched == 2


def test_early_stop_when_no_new_ids(monkeypatch):
    original = parse_listing(FIXTURE.read_text())
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: original)
    monkeypatch.setattr(pagination, "fetch_ajax_page", lambda *a, **kw: (original, 20))
    result = pagination.fetch_paginated_listing(max_pages=5)
    assert result.stop_reason == "no_new_ids"
    assert len(result.events) == 4


def test_invalid_page_bounds_rejected_before_request(monkeypatch):
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: pytest.fail("fetch called"))
    for pages in [0, pagination.MAX_PAGES + 1]:
        with pytest.raises(ValueError, match="max_pages"):
            pagination.fetch_paginated_listing(max_pages=pages)


def test_browser_not_allowed_for_multiple_pages(monkeypatch):
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: pytest.fail("fetch called"))
    with pytest.raises(ValueError, match="HTTP only"):
        pagination.fetch_paginated_listing(max_pages=2, engine="playwright")


def test_failed_ajax_does_not_return_partial_success(monkeypatch):
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: parse_listing(FIXTURE.read_text()))

    def fail(*args, **kwargs):
        raise CryptoEventsError("HTTP 429")

    monkeypatch.setattr(pagination, "fetch_ajax_page", fail)
    with pytest.raises(CryptoEventsError, match="429"):
        pagination.fetch_paginated_listing(max_pages=2)


def test_http_post_is_form_encoded_and_bounded(monkeypatch):
    captured = {}
    response = Mock(status_code=200)
    response.json.return_value = [_ajax_row()]

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return response

    monkeypatch.setattr(pagination.httpx, "post", fake_post)
    records, raw_count = pagination.fetch_ajax_page(20, start_day_utc=1788000000)
    assert len(records) == raw_count == 1
    assert captured["url"] == pagination.AJAX_URL
    assert captured["data"]["offset"] == "20"
    assert captured["data"]["postperpage"] == "20"
    assert captured["data"]["categories[]"] == "16"
    assert captured["data"]["index_filters[start_date]"] == "1788000000"
    assert captured["follow_redirects"] is False


@pytest.mark.parametrize("status", [403, 429, 500, 302])
def test_blocking_http_status_raises(monkeypatch, status):
    monkeypatch.setattr(pagination.httpx, "post", lambda *a, **kw: Mock(status_code=status))
    with pytest.raises(CryptoEventsError, match="HTTP"):
        pagination.fetch_ajax_page(20, start_day_utc=123456)


def test_invalid_json_raises(monkeypatch):
    response = Mock(status_code=200)
    response.json.side_effect = ValueError("invalid JSON")
    monkeypatch.setattr(pagination.httpx, "post", lambda *a, **kw: response)
    with pytest.raises(CryptoEventsError, match="JSON"):
        pagination.fetch_ajax_page(20, start_day_utc=123456)


def test_raw_count_not_filtered_record_count_controls_next_page(monkeypatch):
    original = parse_listing(FIXTURE.read_text())
    monkeypatch.setattr(pagination, "fetch_listing", lambda **kw: original)
    offsets = []

    def extra_page(offset, **kw):
        offsets.append(offset)
        if offset == 20:
            # Twenty raw elements, but two are unparseable/duplicate.
            entries = [_ajax_row(slug=f"event-{i}") for i in range(18)]
            return pagination.parse_ajax_records(entries), 20
        return [], 0

    monkeypatch.setattr(pagination, "fetch_ajax_page", extra_page)
    result = pagination.fetch_paginated_listing(max_pages=3)
    assert offsets == [20, 40]
    assert result.pages_fetched == 3
    assert result.stop_reason == "empty_page"
    assert len(result.events) == len(original) + 18
