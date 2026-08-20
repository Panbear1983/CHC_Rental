"""Manual test pushes are selected, receipt-backed, and ledger-isolated."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from chc_rental.dedup import dedup_key
from chc_rental.models import Allowlist, Listing, Profile, Search, Settings
from chc_rental.notify.telegram import TelegramReceipt, TelegramSendError
from chc_rental.sources.planner import plan_queries
from chc_rental.test_push import TestPushBlocked, deliver_test_push, prepare_test_push
from chc_rental.tui.controller import TuiController

from tests.conftest import make_listing, make_person, make_search

NOW = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)


class RecordingSender:
    def __init__(self, fail_at: int | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self.fail_at = fail_at
        self.error = error

    def send(self, *, telegram_id: int, text: str) -> TelegramReceipt:
        self.calls.append((telegram_id, text))
        if self.fail_at == len(self.calls):
            raise self.error or TelegramSendError("send failed", terminal=True)
        return TelegramReceipt(message_id=str(100 + len(self.calls)), chat_id=str(telegram_id))


def _ready(store) -> None:
    store.save_settings(Settings(live_push_enabled=True))
    # Test pushes deliberately require a real, source-compatible cache. Make
    # generic test profiles source-fetchable, then cache one direct Zillow card
    # per active city without crossing the network.
    with store.edit_allowlist() as allowlist:
        for person in allowlist.people:
            for index, search in enumerate(person.profile.searches):
                if search.active and search.state is None:
                    person.profile.searches[index] = Search.model_validate(
                        {**search.model_dump(), "state": "TX"}
                    )
    queries, _ = plan_queries(store.load_allowlist())
    scope = sorted(
        (
            {"city": query.city.strip().casefold(), "state": query.state.strip().upper()}
            for query in queries
        ),
        key=lambda item: (item["city"], item["state"]),
    )
    records = [
        make_listing(
            source="zillow",
            source_listing_id=f"Z{index}",
            url=f"https://www.zillow.com/homedetails/test-{index}/{index}_zpid/",
            address=f"{index} Source St",
            city=query.city,
            state=query.state,
        )
        for index, query in enumerate(queries, 1)
    ]
    if scope:
        store.cache_raw(
            datetime.now(timezone.utc).date(),
            "zillow",
            records,
            metadata={"query_scope": scope},
        )


def test_selected_recipient_gets_one_summary_with_every_active_preference(store):
    active_one = make_search(name="Downtown", city="Austin")
    paused = make_search(name="Paused secret", active=False)
    active_two = make_search(name="Brooklyn", city="Brooklyn", state="NY")
    selected = make_person(
        222,
        display_name="Selected",
        profile=Profile(searches=[active_one, paused, active_two]),
    )
    store.save_allowlist(Allowlist(people=[make_person(111), selected]))
    _ready(store)
    plan = prepare_test_push(store, 222)
    sender = RecordingSender()
    result = deliver_test_push(store, plan, sender=sender, now_utc=NOW)

    assert result.status == "success"
    assert [telegram_id for telegram_id, _ in sender.calls] == [222]
    body = sender.calls[0][1]
    assert "Downtown" in body and "Brooklyn" in body
    assert "Paused secret" not in body
    assert "https://www.zillow.com/homedetails/" in body
    assert "Normal push time:" in body
    assert "no effect on normal morning delivery" in body
    assert store.last_sent_at(222) is None
    assert len(store.seen_keys(222)) == plan.listing_count
    assert store.seen_keys(111) == set(), "delivery banks must remain per recipient"
    assert store.routine_delivery_days(222, now_utc=NOW) == []

    audit = json.loads(store.test_push_path(NOW.date()).read_text().strip())
    assert audit["telegram_id"] == 222
    assert audit["status"] == "success"
    assert audit["message_ids"] == ["101"]
    assert set(audit["listing_keys"]) == store.seen_keys(222)
    assert audit["repeat_override"] is False
    assert "MANUAL TEST PUSH" not in json.dumps(audit)


def test_successive_test_push_requires_explicit_repeat_and_remains_per_recipient(store):
    people = [make_person(111, display_name="Peter"), make_person(222, display_name="Chun")]
    store.save_allowlist(Allowlist(people=people))
    _ready(store)
    now = datetime.now(timezone.utc)

    first = prepare_test_push(store, 111, now_utc=now)
    assert first.repeat_override is False
    delivered = deliver_test_push(store, first, sender=RecordingSender(), now_utc=now)
    assert delivered.status == "success"

    peter_again = prepare_test_push(store, 111, now_utc=now)
    chun = prepare_test_push(store, 222, now_utc=now)
    assert peter_again.repeat_override is True
    assert "WARNING — REPEAT SEND" in peter_again.confirmation_text
    assert chun.repeat_override is False
    assert chun.listing_count == first.listing_count
    assert store.seen_keys(222) == set()

    repeated = deliver_test_push(
        store,
        peter_again,
        sender=RecordingSender(),
        now_utc=now,
    )
    assert repeated.status == "success"
    assert store.seen_keys(111) == set(delivered.listing_keys)
    events = [
        json.loads(line)
        for line in store.seen_path(111).read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["repeat_override"] is True for event in events)
    assert store.last_sent_at(111) is None


def test_seen_rows_are_filtered_before_the_daily_cap(store):
    search = make_search(state="TX", daily_cap=2)
    store.save_allowlist(
        Allowlist(people=[make_person(111, profile=Profile(searches=[search]))])
    )
    _ready(store)
    now = datetime.now(timezone.utc)
    records = [
        make_listing(
            source="zillow",
            source_listing_id=f"CAP-{index}",
            url=f"https://www.zillow.com/homedetails/cap-{index}/{index}_zpid/",
            address=f"{index} Cap St",
            city="Austin",
            state="TX",
        )
        for index in range(1, 5)
    ]
    store.cache_raw(
        now.date(),
        "zillow",
        records,
        metadata={"query_scope": [{"city": "austin", "state": "TX"}]},
    )
    for raw in records[:2]:
        listing = Listing.model_validate(raw)
        store.mark_seen(
            111,
            dedup_key(listing),
            search_name=search.name,
            url=listing.url,
            now_utc=now,
        )

    plan = prepare_test_push(store, 111, now_utc=now)
    body = "\n".join(plan.messages)

    assert plan.repeat_override is False
    assert plan.listing_count == 2
    assert "3 Cap St" in body and "4 Cap St" in body
    assert "1 Cap St" not in body and "2 Cap St" not in body


def test_delivery_history_change_after_preview_aborts_before_transport(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)
    now = datetime.now(timezone.utc)
    plan = prepare_test_push(store, 111, now_utc=now)
    item = plan.part_items[0][0]
    store.mark_seen(
        111,
        item.key,
        search_name=item.search_name,
        url=item.url,
        now_utc=now,
        channel="scheduled",
    )
    sender = RecordingSender()

    result = deliver_test_push(store, plan, sender=sender, now_utc=now)

    assert result.status == "failed"
    assert "delivery history changed" in result.error
    assert sender.calls == []


def test_preference_change_after_confirmation_aborts_without_send(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)
    plan = prepare_test_push(store, 111)
    with store.edit_allowlist() as allowlist:
        allowlist.get(111).profile.searches[0].price_max += 1

    sender = RecordingSender()
    result = deliver_test_push(store, plan, sender=sender, now_utc=NOW)

    assert result.status == "failed"
    assert "changed" in result.error
    assert sender.calls == []


@pytest.mark.parametrize(
    "person,error",
    [
        (make_person(111, active=False), "paused"),
        (make_person(111, profile=Profile()), "no active preferences"),
    ],
)
def test_paused_recipient_or_empty_active_preferences_are_blocked(store, person, error):
    store.save_allowlist(Allowlist(people=[person]))
    with pytest.raises(TestPushBlocked, match=error):
        prepare_test_push(store, 111)


def test_global_live_switch_is_rechecked_after_confirmation(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)
    plan = prepare_test_push(store, 111)
    store.save_settings(Settings(live_push_enabled=False))
    sender = RecordingSender()

    result = deliver_test_push(store, plan, sender=sender, now_utc=NOW)

    assert result.status == "failed"
    assert "disabled" in result.error
    assert sender.calls == []


def test_controller_blocks_missing_bot_token_before_confirmation(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)

    with pytest.raises(TestPushBlocked, match="bot token"):
        TuiController(store).prepare_test_push(111)


def test_controller_blocks_revoked_bot_token_before_confirmation(store, monkeypatch):
    class RevokedSender:
        def whoami(self):
            raise TelegramSendError(
                "getMe refused: Unauthorized", terminal=True
            )

    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)
    monkeypatch.setattr(
        "chc_rental.tui.controller.build_sender", lambda env_path: RevokedSender()
    )

    with pytest.raises(TestPushBlocked, match="invalid or revoked") as captured:
        TuiController(store).prepare_test_push(111)
    assert "@BotFather" in str(captured.value)


def test_controller_preflight_checks_bot_and_selected_chat(store, monkeypatch):
    calls = []

    class ReadySender:
        def whoami(self):
            calls.append(("getMe", None))
            return {"id": 9001, "username": "chc_test_bot"}

        def get_chat(self, telegram_id):
            calls.append(("getChat", telegram_id))
            return {"id": telegram_id}

    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)
    monkeypatch.setattr(
        "chc_rental.tui.controller.build_sender", lambda env_path: ReadySender()
    )

    plan = TuiController(store).prepare_test_push(111)
    assert plan.telegram_id == 111
    assert calls == [("getMe", None), ("getChat", 111)]


def test_ambiguous_failure_is_not_retried(store):
    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)
    plan = prepare_test_push(store, 111)
    sender = RecordingSender(
        fail_at=1,
        error=TelegramSendError("network response was lost", ambiguous=True),
    )

    result = deliver_test_push(store, plan, sender=sender, now_utc=NOW)

    assert result.status == "uncertain"
    assert len(sender.calls) == 1


def test_unverifiable_chat_receipt_is_uncertain_and_not_retried(store):
    class WrongChatSender:
        calls = 0

        def send(self, *, telegram_id: int, text: str) -> TelegramReceipt:
            self.calls += 1
            return TelegramReceipt(message_id="100", chat_id="999")

    store.save_allowlist(Allowlist(people=[make_person(111)]))
    _ready(store)
    plan = prepare_test_push(store, 111)
    sender = WrongChatSender()

    result = deliver_test_push(store, plan, sender=sender, now_utc=NOW)

    assert result.status == "uncertain"
    assert sender.calls == 1
    assert result.parts_accepted == 0


def test_long_summary_splits_only_between_preferences_and_reports_partial(store):
    searches = [
        make_search(
            name=f"Preference {index} " + ("x" * 75),
            city=f"Austin {index}",
        )
        for index in range(1, 4)
    ]
    store.save_allowlist(
        Allowlist(people=[make_person(111, profile=Profile(searches=searches))])
    )
    _ready(store)
    plan = prepare_test_push(store, 111, max_message_chars=700)
    assert len(plan.messages) > 1
    assert all("part" in message for message in plan.messages)
    for index, search in enumerate(searches, 1):
        assert sum(search.name in message for message in plan.messages) == 1

    sender = RecordingSender(fail_at=2)
    result = deliver_test_push(store, plan, sender=sender, now_utc=NOW)
    assert result.status == "partial"
    assert result.parts_accepted == 1
    assert len(sender.calls) == 2
    assert store.seen_keys(111) == {
        item.key for item in plan.part_items[0]
    }


def test_test_push_sends_more_than_five_matching_cards_with_direct_links(store):
    search = make_search(state="TX", daily_cap=25)
    store.save_allowlist(
        Allowlist(people=[make_person(111, profile=Profile(searches=[search]))])
    )
    _ready(store)
    scope = [{"city": "austin", "state": "TX"}]
    records = [
        make_listing(
            source="zillow",
            source_listing_id=f"Z{index}",
            url=f"https://www.zillow.com/homedetails/new-{index}/{index}_zpid/",
            address=f"{index} Newest St",
            city="Austin",
            state="TX",
        )
        for index in range(1, 7)
    ]
    store.cache_raw(
        datetime.now(timezone.utc).date(),
        "zillow",
        records,
        metadata={"query_scope": scope},
    )

    plan = prepare_test_push(store, 111)
    body = "\n".join(plan.messages)

    assert plan.listing_count == 6
    assert body.count("https://www.zillow.com/homedetails/new-") == 6
    assert body.index("1 Newest St") < body.index("6 Newest St")


def test_test_push_accepts_a_cache_whose_city_scope_is_a_safe_superset(store):
    search = make_search(city="Brooklyn", state="NY")
    store.save_allowlist(
        Allowlist(people=[make_person(111, profile=Profile(searches=[search]))])
    )
    _ready(store)
    store.cache_raw(
        datetime.now(timezone.utc).date(),
        "zillow",
        [
            make_listing(
                source="zillow",
                source_listing_id="BROOKLYN-1",
                url="https://www.zillow.com/homedetails/brooklyn/1_zpid/",
                address="1 Brooklyn Cache St",
                city="Brooklyn",
                state="NY",
            )
        ],
        metadata={
            "query_scope": [
                {"city": "brooklyn", "state": "NY"},
                {"city": "nyc", "state": "NY"},
            ]
        },
    )

    plan = prepare_test_push(store, 111)

    assert plan.listing_count == 1
    assert "https://www.zillow.com/homedetails/brooklyn/1_zpid/" in plan.messages[0]


def test_test_push_rejects_a_cache_missing_a_required_city(store):
    searches = [
        make_search(name="Brooklyn", city="Brooklyn", state="NY"),
        make_search(name="Queens", city="Queens", state="NY"),
    ]
    store.save_allowlist(
        Allowlist(people=[make_person(111, profile=Profile(searches=searches))])
    )
    _ready(store)
    store.cache_raw(
        datetime.now(timezone.utc).date(),
        "zillow",
        [],
        metadata={"query_scope": [{"city": "brooklyn", "state": "NY"}]},
    )

    with pytest.raises(TestPushBlocked, match="no compatible Zillow cache"):
        prepare_test_push(store, 111)


def test_another_members_uncovered_city_does_not_block_selected_test_push(store):
    selected = make_person(
        111,
        profile=Profile(
            searches=[make_search(name="Brooklyn", city="Brooklyn", state="NY")]
        ),
    )
    unrelated = make_person(
        222,
        profile=Profile(
            searches=[
                make_search(
                    name="Unsupported combined text",
                    city="Brooklyn + New York City",
                    state="NY",
                )
            ]
        ),
    )
    store.save_allowlist(Allowlist(people=[selected, unrelated]))
    _ready(store)
    store.cache_raw(
        datetime.now(timezone.utc).date(),
        "zillow",
        [
            make_listing(
                source="zillow",
                source_listing_id="BROOKLYN-1",
                url="https://www.zillow.com/homedetails/brooklyn/1_zpid/",
                address="1 Selected St",
                city="Brooklyn",
                state="NY",
            )
        ],
        metadata={"query_scope": [{"city": "brooklyn", "state": "NY"}]},
    )

    plan = prepare_test_push(store, 111)

    assert plan.telegram_id == 111
    assert plan.listing_count == 1


def test_test_push_audit_uses_normal_retention(store):
    old = datetime(2025, 12, 1, tzinfo=timezone.utc)
    fresh = datetime(2026, 1, 15, tzinfo=timezone.utc)
    store.record_test_push(old, {"telegram_id": 111, "status": "success"})
    store.record_test_push(fresh, {"telegram_id": 111, "status": "success"})

    removed = store.prune(today=fresh.date(), settings=Settings(rejected_retention_days=30))

    assert removed["test_pushes"] == 1
    assert not store.test_push_path(old.date()).exists()
    assert store.test_push_path(fresh.date()).exists()
