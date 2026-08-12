"""Apify HTTP contract and durable asynchronous source-run behavior."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from chc_rental.incremental import IncrementalCollector
from chc_rental.models import Allowlist, Profile
from chc_rental.sources.apify import ApifyClient, ApifyRunState
from chc_rental.sources.base import SourceAuthError, SourceRateLimitError
from chc_rental.sources.zillow import ZillowRentalAdapter

from tests.conftest import make_person, make_search

NOW = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)  # 10:00 New York
BOUNDS = {"west": -98.0, "east": -97.0, "south": 30.0, "north": 31.0}
RAW = {
    "zpid": "123456",
    "detailUrl": "/homedetails/100-Main-St-Austin-TX/123456_zpid/",
    "statusType": "FOR_RENT",
    "addressStreet": "100 Main St",
    "addressCity": "Austin",
    "addressState": "TX",
    "addressZipcode": "78701",
    "unformattedPrice": 2200,
    "beds": 2,
    "baths": 1.5,
    "area": 900,
    "homeType": "APARTMENT",
}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_apify_client_uses_authorization_header_and_keeps_token_out_of_url():
    captured = {}

    def opener(request, timeout, context):
        captured["request"] = request
        payload = {
            "data": {
                "id": "run-1",
                "status": "RUNNING",
                "defaultDatasetId": "dataset-1",
            }
        }
        return Response(json.dumps(payload).encode())

    client = ApifyClient(token="SECRET", opener=opener)
    state = client.start_actor(
        "owner~actor", {"input": True}, max_total_charge_usd=0.25
    )
    request = captured["request"]
    assert request.get_header("Authorization") == "Bearer SECRET"
    assert "SECRET" not in request.full_url
    assert "maxTotalChargeUsd=0.25" in request.full_url
    assert state.run_id == "run-1" and not state.terminal
    assert "SECRET" not in repr(client)


@pytest.mark.parametrize("code,error", [(401, SourceAuthError), (429, SourceRateLimitError)])
def test_apify_client_maps_auth_and_rate_errors(code, error):
    def opener(request, timeout, context):
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            "boom",
            {"Retry-After": "4"},
            io.BytesIO(b'{"error":{"message":"safe"}}'),
        )

    with pytest.raises(error):
        ApifyClient(token="SECRET", opener=opener).get_run("run-1")


class FakeClient:
    def __init__(self, *, start_status="RUNNING", poll_status="SUCCEEDED", raw=None):
        self.start_status = start_status
        self.poll_status = poll_status
        self.raw = list(raw if raw is not None else [RAW])
        self.start_calls = 0
        self.poll_calls = 0
        self.dataset_calls = 0

    def start_actor(self, actor, payload, *, max_total_charge_usd):
        self.start_calls += 1
        return ApifyRunState(
            f"remote-{self.start_calls}", self.start_status, "dataset-1", None
        )

    def get_run(self, run_id):
        self.poll_calls += 1
        return ApifyRunState(
            run_id, self.poll_status, "dataset-1", 0.06, "done"
        )

    def get_dataset(self, dataset_id):
        self.dataset_calls += 1
        return self.raw


def prepared_store(store, *, searches=1):
    items = [
        make_search(
            name=f"Search {index}",
            state="TX",
            price_min=1000 + index * 100,
            search_id=f"00000000-0000-4000-8000-{index + 1:012d}",
        )
        for index in range(searches)
    ]
    store.save_allowlist(
        Allowlist(schema_version=1, people=[make_person(111, profile=Profile(searches=items))])
    )
    store.migrate_config_v2()
    store.migrate_alert_ledger()
    settings = store.load_settings().model_copy(
        update={
            "incremental_alerts_enabled": True,
            "zillow_enabled": True,
            "incremental_max_new_starts_per_cycle": 5,
            "source_daily_request_budgets": {"zillow": 5},
        }
    )
    return settings


def collector(store, settings, client, *, results_limit=25):
    return IncrementalCollector(
        store,
        settings=settings,
        adapter=ZillowRentalAdapter(
            token="SECRET",
            results_limit=results_limit,
            bounds_resolver=lambda query: BOUNDS,
        ),
        client=client,
        worker_id="test-worker",
    )


def test_restart_resumes_persisted_apify_run_without_second_start(store):
    settings = prepared_store(store)
    client = FakeClient(start_status="RUNNING")
    first = collector(store, settings, client).cycle(now_utc=NOW)
    assert first.started == 1 and first.collections[-1].status == "running"
    assert client.start_calls == 1

    second = collector(store, settings, client).cycle(now_utc=NOW + timedelta(minutes=15))
    assert second.resumed == 1
    assert second.collections[0].status == "succeeded"
    assert len(second.records) == 1
    assert client.start_calls == 1 and client.poll_calls == 1


def test_terminal_start_response_can_finish_in_the_same_cycle(store):
    settings = prepared_store(store)
    client = FakeClient(start_status="SUCCEEDED")
    report = collector(store, settings, client).cycle(now_utc=NOW)
    assert report.collections[0].status == "succeeded"
    assert report.collections[0].raw_result_count == 1
    assert client.dataset_calls == 1


def test_result_limit_marks_positive_window_truncated(store):
    settings = prepared_store(store)
    client = FakeClient(start_status="SUCCEEDED", raw=[RAW])
    result = collector(store, settings, client, results_limit=1).cycle(now_utc=NOW)
    assert result.collections[0].truncated is True


def test_source_budget_is_shared_fairly_across_query_scopes(store):
    settings = prepared_store(store, searches=2).model_copy(
        update={"source_daily_request_budgets": {"zillow": 1}}
    )
    client = FakeClient(start_status="RUNNING")
    report = collector(store, settings, client).cycle(now_utc=NOW)
    assert report.started == 1
    assert client.start_calls == 1
    assert any(item.status == "budget_deferred" for item in report.collections)
    assert store.quota_used(NOW.date(), "zillow") == 1


def test_many_missed_intervals_collapse_into_one_new_start(store):
    settings = prepared_store(store)
    first_client = FakeClient(start_status="SUCCEEDED")
    first = collector(store, settings, first_client).cycle(now_utc=NOW)
    assert first.started == 1

    first_client.start_status = "RUNNING"
    later = collector(store, settings, first_client).cycle(now_utc=NOW + timedelta(hours=12))
    assert later.started == 1 and first_client.start_calls == 2


def test_global_kill_switch_prevents_new_paid_start(store):
    settings = prepared_store(store).model_copy(update={"incremental_alerts_enabled": False})
    client = FakeClient()
    report = collector(store, settings, client).cycle(now_utc=NOW)
    assert client.start_calls == 0 and report.started == 0
    assert any("kill switch" in warning for warning in report.warnings)
