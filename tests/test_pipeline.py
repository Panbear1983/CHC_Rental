"""End-to-end pipeline behaviour, with a regression test per audit defect."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chc_rental.dedup import dedup_key
from chc_rental.models import Allowlist, Listing, Profile, Settings
from chc_rental.pipeline import deliver, plan_pushes, validate_records

from tests.conftest import DUE_NOW, make_listing, make_person, make_search


class RecordingSender:
    """Captures sends; raises for ids listed in ``fail_for``."""

    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    def send(self, *, telegram_id: int, text: str) -> None:
        if telegram_id in self.fail_for:
            raise RuntimeError("transport exploded")
        self.sent.append((telegram_id, text))


def listings(*raws) -> list[Listing]:
    return [Listing.model_validate(raw) for raw in raws]


def test_a_due_person_is_planned_one_matching_listing(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    assert result.summary["planned_pushes"] == 1
    assert result.planned[0].telegram_id == 111


def test_the_listing_url_is_in_the_rendered_push(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    assert "https://example.com/listing/1" in result.planned[0].render()


def test_nothing_is_planned_before_the_local_delivery_time(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    too_early = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # 07:00 New York
    result = plan_pushes(store, listings(make_listing()), now_utc=too_early)
    assert result.summary["people_due"] == 0
    assert result.planned == []


def test_an_inactive_search_is_skipped(store):
    person = make_person(111, profile=Profile(searches=[make_search(active=False)]))
    store.save_allowlist(Allowlist(people=[person]))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    assert result.planned == []


def test_daily_cap_limits_one_search(store):
    person = make_person(111, profile=Profile(searches=[make_search(daily_cap=2)]))
    store.save_allowlist(Allowlist(people=[person]))
    many = listings(
        *[make_listing(source_listing_id=f"L{i}", address=f"{i} Main St") for i in range(5)]
    )
    result = plan_pushes(store, many, now_utc=DUE_NOW)
    assert result.summary["planned_pushes"] == 2


# --- regressions for the 2026-08-10 audit -----------------------------------


def test_a_removed_person_receives_nothing(store):
    """The old pipeline filtered on the profile's flag and never joined the
    allowlist, so de-allowlisted users kept getting listings."""
    store.save_allowlist(
        Allowlist(people=[make_person(111), make_person(222, active=False)])
    )
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    recipients = {item.telegram_id for item in result.planned}
    assert recipients == {111}
    assert 222 not in recipients


def test_two_searches_matching_one_listing_send_it_once(store):
    """The old build had no intra-run dedup and double-charged the budget."""
    person = make_person(
        111,
        profile=Profile(
            searches=[make_search(name="Downtown"), make_search(name="Backup")]
        ),
    )
    store.save_allowlist(Allowlist(people=[person]))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    assert result.summary["listings_unique"] == 1
    assert result.summary["planned_pushes"] == 1


def test_two_sources_for_one_rental_send_one_direct_zillow_link(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    rentcast = make_listing(
        source="rentcast",
        url="https://www.google.com/maps/search/?api=1&query=100+Main+St",
        address="100 Main Street",
    )
    zillow = make_listing(
        source="zillow",
        source_listing_id="123_zpid",
        url="https://www.zillow.com/homedetails/100-main/123_zpid/",
        address="100 Main St.",
    )
    result = plan_pushes(store, listings(rentcast, zillow), now_utc=DUE_NOW)
    assert result.summary["listings_unique"] == 1
    assert result.summary["planned_pushes"] == 1
    assert result.planned[0].listing.source == "zillow"
    assert "zillow.com" in result.planned[0].listing.url


def test_one_malformed_record_does_not_discard_the_good_ones(store):
    """The old dry_run raised on the first bad record and kept none of them."""
    raws = [
        make_listing(source_listing_id="ok1", address="1 Main St"),
        make_listing(source_listing_id="bad", address="2 Main St", price=-5),
        make_listing(source_listing_id="ok2", address="3 Main St"),
    ]
    valid, rejected = validate_records(store, raws, day=DUE_NOW.date(), source="feed")
    assert len(valid) == 2
    assert rejected == 1
    assert store.rejected_count(DUE_NOW.date()) == 1


def test_a_listing_without_a_link_is_rejected_not_pushed(store):
    raws = [make_listing(url="not-a-link")]
    valid, rejected = validate_records(store, raws, day=DUE_NOW.date(), source="feed")
    assert valid == [] and rejected == 1


def test_already_seen_listings_are_not_planned_again(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    raw = make_listing()
    store.mark_seen(
        111, dedup_key(Listing.model_validate(raw)), search_name="Downtown", url=raw["url"]
    )
    result = plan_pushes(store, listings(raw), now_utc=DUE_NOW)
    assert result.planned == []


def test_a_legacy_rentcast_seen_key_suppresses_the_same_zillow_rental(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    store.mark_seen(
        111,
        "v2:rentcast::austin::100%20main%20st:",
        search_name="Downtown",
        url="https://www.google.com/maps/search/?api=1&query=100+Main+St",
    )
    result = plan_pushes(
        store,
        listings(make_listing(source="zillow", url="https://www.zillow.com/123_zpid/")),
        now_utc=DUE_NOW,
    )
    assert result.planned == []


def test_same_address_in_two_cities_reaches_both_subscribers(store):
    """The old dedup key omitted city, so the second person got nothing."""
    austin = make_person(111, profile=Profile(searches=[make_search(city="Austin")]))
    dallas = make_person(222, profile=Profile(searches=[make_search(city="Dallas")]))
    store.save_allowlist(Allowlist(people=[austin, dallas]))
    result = plan_pushes(
        store,
        listings(
            make_listing(city="Austin", price=1500),
            make_listing(city="Dallas", price=2400),
        ),
        now_utc=DUE_NOW,
    )
    assert result.summary["listings_unique"] == 2
    assert {item.telegram_id for item in result.planned} == {111, 222}


# --- delivery ---------------------------------------------------------------


def test_dry_run_sends_nothing_and_records_nothing(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    sender = RecordingSender()
    result = deliver(store, result, sender=sender, live=False)
    assert sender.sent == []
    assert store.seen_keys(111) == set()
    assert result.summary["delivery"] == "dry-run"


def test_live_requires_the_settings_flag(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    store.save_settings(Settings(live_push_enabled=False))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    sender = RecordingSender()
    deliver(store, result, sender=sender, live=True)
    assert sender.sent == [], "the config flag must gate live sending"


def test_live_send_marks_seen_only_after_success(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    store.save_settings(Settings(live_push_enabled=True))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    sender = RecordingSender()
    result = deliver(store, result, sender=sender, live=True)
    assert len(sender.sent) == 1
    assert result.sent == 1
    assert len(store.seen_keys(111)) == 1


def test_a_failed_send_is_not_marked_seen(store):
    """Recording before sending would silently drop the listing forever."""
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    store.save_settings(Settings(live_push_enabled=True))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    result = deliver(store, result, sender=RecordingSender(fail_for={111}), live=True)
    assert result.failed == 1 and result.sent == 0
    assert store.seen_keys(111) == set(), "a failed send must remain retryable"


def test_a_person_removed_between_planning_and_sending_gets_nothing(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    store.save_settings(Settings(live_push_enabled=True))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    store.save_allowlist(Allowlist(people=[make_person(111, active=False)]))
    sender = RecordingSender()
    deliver(store, result, sender=sender, live=True)
    assert sender.sent == []


def test_a_sent_listing_is_never_planned_again_the_next_day(store):
    """The old ledger let mark_failed resurrect a sent row for three deliveries."""
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    store.save_settings(Settings(live_push_enabled=True))
    sender = RecordingSender()
    first = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    deliver(store, first, sender=sender, live=True)

    tomorrow = DUE_NOW + timedelta(days=1)
    second = plan_pushes(store, listings(make_listing()), now_utc=tomorrow)
    deliver(store, second, sender=sender, live=True)
    assert len(sender.sent) == 1, "the same listing must not be delivered twice"


def test_no_results_notice_only_when_opted_in(store):
    quiet = make_person(111, profile=Profile(searches=[make_search(city="Nowhere")]))
    loud = make_person(
        222,
        profile=Profile(searches=[make_search(city="Nowhere")], notify_on_no_results=True),
    )
    store.save_allowlist(Allowlist(people=[quiet, loud]))
    result = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    assert result.no_results_for == [222]


def test_no_results_notice_is_suppressed_for_an_incomplete_source_pool(store):
    person = make_person(
        111,
        profile=Profile(searches=[make_search(city="Nowhere")], notify_on_no_results=True),
    )
    store.save_allowlist(Allowlist(people=[person]))
    result = plan_pushes(
        store,
        listings(make_listing()),
        now_utc=DUE_NOW,
        allow_no_results=False,
    )
    assert result.no_results_for == []
    assert result.summary["no_results_suppressed"] is True


def test_a_delivered_no_results_notice_does_not_repeat_within_the_day(store):
    """Regression: notices never advanced the due-gate, so the hourly job
    re-sent "no new rentals" every hour until local midnight."""
    person = make_person(
        111,
        profile=Profile(searches=[make_search(city="Nowhere")], notify_on_no_results=True),
    )
    store.save_allowlist(Allowlist(people=[person]))
    store.save_settings(Settings(live_push_enabled=True))
    sender = RecordingSender()

    first = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    deliver(store, first, sender=sender, live=True, now_utc=DUE_NOW)
    assert len(sender.sent) == 1
    assert first.summary["no_results_sent"] == 1

    an_hour_later = DUE_NOW + timedelta(hours=1)
    second = plan_pushes(store, listings(make_listing()), now_utc=an_hour_later)
    deliver(store, second, sender=sender, live=True, now_utc=an_hour_later)
    assert len(sender.sent) == 1, "the notice must not repeat within one local day"
    assert second.no_results_for == []

    next_day = DUE_NOW + timedelta(days=1)
    third = plan_pushes(store, listings(make_listing()), now_utc=next_day)
    assert third.no_results_for == [111], "but the next local day is due again"


def test_a_failed_notice_does_not_advance_the_due_gate(store):
    person = make_person(
        111,
        profile=Profile(searches=[make_search(city="Nowhere")], notify_on_no_results=True),
    )
    store.save_allowlist(Allowlist(people=[person]))
    store.save_settings(Settings(live_push_enabled=True))

    first = plan_pushes(store, listings(make_listing()), now_utc=DUE_NOW)
    result = deliver(store, first, sender=RecordingSender(fail_for={111}), live=True, now_utc=DUE_NOW)
    assert result.summary["no_results_failed"] == 1

    an_hour_later = DUE_NOW + timedelta(hours=1)
    second = plan_pushes(store, listings(make_listing()), now_utc=an_hour_later)
    assert second.no_results_for == [111], "a failed notice must stay retryable"
