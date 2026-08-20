"""Small authenticated Apify run client for durable incremental collection.

The asynchronous API returns a run ID immediately. CHC persists that ID before
polling so a process restart resumes the paid run instead of routinely starting
another one. Apify's Run Actor endpoint does not document an idempotency key, so
the narrow network-accepted/local-not-recorded window is treated as uncertain
rather than blindly retried by the scheduler.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import Message
from typing import Any, Callable

from chc_rental.net import ssl_context
from chc_rental.sources.base import (
    SourceAuthError,
    SourceRateLimitError,
    SourceUnavailableError,
)

APIFY_API = "https://api.apify.com/v2"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
FAILED_STATUSES = {"FAILED", "TIMED-OUT", "ABORTED"}


@dataclass(frozen=True)
class ApifyRunState:
    run_id: str
    status: str
    default_dataset_id: str | None
    usage_total_usd: float | None
    status_message: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"


def _retry_after(headers: Message | dict[str, str] | None) -> float | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return ""
    message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
    return f": {str(message)[:300]}" if message else ""


@dataclass
class ApifyClient:
    token: str = field(repr=False)
    timeout: float = 30.0
    opener: Callable[..., Any] = field(default=urllib.request.urlopen, repr=False)

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with self.opener(
                request, timeout=self.timeout, context=ssl_context()
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code in (401, 403):
                raise SourceAuthError(
                    f"Apify rejected the token (HTTP {exc.code}{detail})"
                ) from None
            if exc.code == 429:
                raise SourceRateLimitError(
                    "Apify rate limit (HTTP 429)",
                    retry_after=_retry_after(exc.headers),
                ) from None
            raise SourceUnavailableError(f"Apify HTTP {exc.code}{detail}") from None
        except urllib.error.URLError as exc:
            raise SourceUnavailableError(f"Apify unreachable: {exc.reason}") from None
        except (OSError, UnicodeError, ValueError) as exc:
            raise SourceUnavailableError(
                f"Apify unreadable response: {type(exc).__name__}"
            ) from None

    @staticmethod
    def _run_state(payload: Any) -> ApifyRunState:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise SourceUnavailableError("Apify response did not contain a run object")
        run_id = str(data.get("id") or "").strip()
        status = str(data.get("status") or "").strip().upper()
        if not run_id or not status:
            raise SourceUnavailableError("Apify run object lacked id or status")
        raw_cost = data.get("usageTotalUsd")
        try:
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost = None
        return ApifyRunState(
            run_id=run_id,
            status=status,
            default_dataset_id=(
                str(data["defaultDatasetId"]) if data.get("defaultDatasetId") else None
            ),
            usage_total_usd=cost,
            status_message=(str(data["statusMessage"])[:300] if data.get("statusMessage") else None),
        )

    def start_actor(
        self,
        actor: str,
        payload: dict[str, Any],
        *,
        max_total_charge_usd: float | None,
    ) -> ApifyRunState:
        actor_id = urllib.parse.quote(actor, safe="~")
        params: dict[str, str] = {"restartOnError": "false"}
        if max_total_charge_usd is not None and max_total_charge_usd > 0:
            params["maxTotalChargeUsd"] = f"{max_total_charge_usd:g}"
        url = f"{APIFY_API}/actors/{actor_id}/runs?{urllib.parse.urlencode(params)}"
        return self._run_state(self._request("POST", url, payload=payload))

    def get_run(self, run_id: str) -> ApifyRunState:
        encoded = urllib.parse.quote(run_id, safe="")
        return self._run_state(self._request("GET", f"{APIFY_API}/actor-runs/{encoded}"))

    def account_limits(self) -> dict[str, Any]:
        """Monthly subscription usage against its ceiling.

        The daily job discovers the ceiling the expensive way — every actor run
        starts returning HTTP 403 ``platform-feature-disabled`` and the whole
        product stops, as it did on 2026-08-20. This is the cheap way to see it
        coming: a free read of what has been spent and when the cycle resets.
        """
        payload = self._request("GET", f"{APIFY_API}/users/me/limits")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise SourceUnavailableError("Apify limits response had no data object")
        limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
        current = data.get("current") if isinstance(data.get("current"), dict) else {}
        cycle = (
            data.get("monthlyUsageCycle")
            if isinstance(data.get("monthlyUsageCycle"), dict)
            else {}
        )
        used = current.get("monthlyUsageUsd")
        cap = limits.get("maxMonthlyUsageUsd")
        return {
            "monthly_usage_usd": float(used) if isinstance(used, (int, float)) else None,
            "monthly_cap_usd": float(cap) if isinstance(cap, (int, float)) else None,
            "cycle_start": cycle.get("startAt"),
            "cycle_end": cycle.get("endAt"),
        }

    def get_dataset(self, dataset_id: str) -> list[Any]:
        encoded = urllib.parse.quote(dataset_id, safe="")
        payload = self._request(
            "GET", f"{APIFY_API}/datasets/{encoded}/items?clean=true&format=json"
        )
        if not isinstance(payload, list):
            raise SourceUnavailableError("Apify dataset response was not a list")
        return payload
