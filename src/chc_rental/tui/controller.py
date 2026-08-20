"""Service layer the TUI screens call into.

Every mutation goes through `Store.edit_allowlist`, which holds one lock across
the whole read-modify-write. No screen may touch a file directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from chc_rental.models import AllowlistEntry, DeliveryMode, Profile, Search, Settings
from chc_rental.delivery_history import RoutineDeliveryDay
from chc_rental.incremental import IncrementalCollector
from chc_rental.fetch import load_daily_cached, scrape_day
from chc_rental.notification_schedule import push_precedes_scrape
from chc_rental.notify.telegram import TelegramSendError, build_sender
from chc_rental.outbox import process_incremental_report
from chc_rental.operations import incremental_cost_status, incremental_rollout_readiness
from chc_rental.pipeline import PipelineResult, plan_pushes
from chc_rental.sources.apify import ApifyClient
from chc_rental.sources import KNOWN_SOURCES
from chc_rental.sources.zillow import ZillowRentalAdapter, load_apify_token
from chc_rental.store import Store, StoreError
from chc_rental.test_push import (
    TestPushBlocked,
    TestPushPlan,
    TestPushResult,
    deliver_test_push,
    prepare_test_push,
)
from chc_rental.tui.forms import parse_search_form


class DuplicateError(ValueError):
    """Raised when a telegram id or search name is already taken."""


class NotFoundError(ValueError):
    """Raised when a person or search does not exist."""


@dataclass(frozen=True)
class RecipientMutationResult:
    """Visible outcome of an allowlist edit/removal and its state cleanup."""

    old_telegram_id: int
    new_telegram_id: int | None
    seen_ledgers_removed: int = 0
    outbox_rows_cancelled: int = 0
    canary_changed: bool = False

    @property
    def id_changed(self) -> bool:
        return (
            self.new_telegram_id is not None
            and self.new_telegram_id != self.old_telegram_id
        )


class TuiController:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ people
    def list_people(self) -> list[AllowlistEntry]:
        return self.store.load_allowlist().people

    def get_person(self, telegram_id: int) -> AllowlistEntry:
        person = self.store.load_allowlist().get(telegram_id)
        if person is None:
            raise NotFoundError(f"no allowlisted person with id {telegram_id}")
        return person

    def add_person(self, telegram_id: int, display_name: str) -> None:
        with self.store.edit_allowlist() as allowlist:
            if allowlist.get(telegram_id) is not None:
                raise DuplicateError(f"telegram id {telegram_id} is already on the allowlist")
            allowlist.people.append(
                AllowlistEntry(
                    telegram_id=telegram_id, display_name=display_name, profile=Profile()
                )
            )

    def set_person_active(self, telegram_id: int, active: bool) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            person.active = active

    def routine_delivery_diary(
        self,
        telegram_id: int,
        *,
        now_utc: datetime | None = None,
    ) -> list[RoutineDeliveryDay]:
        """Return accepted/uncertain routine history for one current member."""
        self.get_person(telegram_id)
        return self.store.routine_delivery_days(telegram_id, now_utc=now_utc)

    def update_person(
        self,
        old_telegram_id: int,
        new_telegram_id: int,
        display_name: str,
    ) -> RecipientMutationResult:
        """Edit one member; an ID correction starts recipient history fresh."""
        name = display_name.strip()
        if new_telegram_id <= 0 or not name:
            raise ValueError(
                "Telegram ID must be a positive whole number and display name is required."
            )
        allowlist = self.store.load_allowlist()
        if allowlist.get(old_telegram_id) is None:
            raise NotFoundError(
                f"no allowlisted person with id {old_telegram_id}"
            )
        if (
            new_telegram_id != old_telegram_id
            and allowlist.get(new_telegram_id) is not None
        ):
            raise DuplicateError(
                f"telegram id {new_telegram_id} is already on the allowlist"
            )

        if new_telegram_id == old_telegram_id:
            with self.store.edit_allowlist() as current:
                person = current.get(old_telegram_id)
                if person is None:
                    raise NotFoundError(
                        f"no allowlisted person with id {old_telegram_id}"
                    )
                person.display_name = name
            return RecipientMutationResult(old_telegram_id, new_telegram_id)

        affected_ids = {old_telegram_id, new_telegram_id}
        cancelled = self.store.event_store().cancel_recipient_outbox(
            affected_ids,
            now_utc=datetime.now(timezone.utc),
            reason=(
                f"recipient Telegram ID changed from {old_telegram_id} "
                f"to {new_telegram_id}"
            ),
        )
        with self.store.edit_allowlist() as current:
            if current.get(new_telegram_id) is not None:
                raise DuplicateError(
                    f"telegram id {new_telegram_id} is already on the allowlist"
                )
            person = current.get(old_telegram_id)
            if person is None:
                raise NotFoundError(
                    f"no allowlisted person with id {old_telegram_id}"
                )
            person.telegram_id = new_telegram_id
            person.display_name = name

        canary_changed = False
        with self.store.edit_settings() as settings:
            if old_telegram_id in settings.incremental_canary_telegram_ids:
                remapped: list[int] = []
                for telegram_id in settings.incremental_canary_telegram_ids:
                    candidate = (
                        new_telegram_id
                        if telegram_id == old_telegram_id
                        else telegram_id
                    )
                    if candidate not in remapped:
                        remapped.append(candidate)
                settings.incremental_canary_telegram_ids = remapped
                canary_changed = True
        removed = self.store.purge_seen_ledgers(affected_ids)
        return RecipientMutationResult(
            old_telegram_id,
            new_telegram_id,
            seen_ledgers_removed=removed,
            outbox_rows_cancelled=cancelled,
            canary_changed=canary_changed,
        )

    def remove_person(self, telegram_id: int) -> RecipientMutationResult:
        if self.store.load_allowlist().get(telegram_id) is None:
            raise NotFoundError(f"no allowlisted person with id {telegram_id}")
        cancelled = self.store.event_store().cancel_recipient_outbox(
            {telegram_id},
            now_utc=datetime.now(timezone.utc),
            reason=f"recipient {telegram_id} removed from the allowlist",
        )
        canary_changed = False
        with self.store.edit_settings() as settings:
            if telegram_id in settings.incremental_canary_telegram_ids:
                settings.incremental_canary_telegram_ids = [
                    item
                    for item in settings.incremental_canary_telegram_ids
                    if item != telegram_id
                ]
                canary_changed = True
        with self.store.edit_allowlist() as allowlist:
            if allowlist.get(telegram_id) is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            allowlist.people = [p for p in allowlist.people if p.telegram_id != telegram_id]
        removed = self.store.purge_seen_ledgers({telegram_id})
        return RecipientMutationResult(
            telegram_id,
            None,
            seen_ledgers_removed=removed,
            outbox_rows_cancelled=cancelled,
            canary_changed=canary_changed,
        )

    # ---------------------------------------------------------------- searches
    def list_searches(self, telegram_id: int) -> list[Search]:
        return self.get_person(telegram_id).profile.searches

    def add_search(self, telegram_id: int, form_data: dict) -> None:
        search = parse_search_form(form_data)
        search.search_id = str(uuid4())
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            existing = {s.name.lower() for s in person.profile.searches}
            if search.name.lower() in existing:
                raise DuplicateError(f"this profile already has a search named {search.name!r}")
            person.profile.searches.append(search)

    def update_search(self, telegram_id: int, index: int, form_data: dict) -> None:
        search = parse_search_form(form_data)
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            if not 0 <= index < len(person.profile.searches):
                raise NotFoundError(f"no search at position {index}")
            clashes = {
                s.name.lower()
                for position, s in enumerate(person.profile.searches)
                if position != index
            }
            if search.name.lower() in clashes:
                raise DuplicateError(f"this profile already has a search named {search.name!r}")
            search.search_id = person.profile.searches[index].search_id
            person.profile.searches[index] = search

    def delete_search(self, telegram_id: int, index: int) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            if not 0 <= index < len(person.profile.searches):
                raise NotFoundError(f"no search at position {index}")
            person.profile.searches.pop(index)

    def toggle_search(self, telegram_id: int, index: int) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            if not 0 <= index < len(person.profile.searches):
                raise NotFoundError(f"no search at position {index}")
            search = person.profile.searches[index]
            search.active = not search.active

    # ----------------------------------------------------------------- profile
    def update_delivery(
        self,
        telegram_id: int,
        *,
        delivery_time: str,
        timezone_name: str,
        delivery_mode: str,
        quiet_hours_start: str | None,
        quiet_hours_end: str | None,
        notify_on_no_results: bool,
    ) -> None:
        with self.store.edit_allowlist() as allowlist:
            person = allowlist.get(telegram_id)
            if person is None:
                raise NotFoundError(f"no allowlisted person with id {telegram_id}")
            person.profile.delivery_time = delivery_time
            person.profile.timezone = timezone_name
            person.profile.delivery_mode = DeliveryMode(delivery_mode)
            person.profile.quiet_hours_start = quiet_hours_start
            person.profile.quiet_hours_end = quiet_hours_end
            person.profile.notify_on_no_results = notify_on_no_results

    def update_incremental_settings(
        self,
        *,
        incremental_alerts_enabled: bool,
        zillow_enabled: bool,
        zillow_terms_confirmed: bool,
        zillow_actor: str,
        zillow_incremental_interval_minutes: int,
        zillow_daily_request_budget: int,
        zillow_results_limit: int,
        zillow_max_charge_usd: float,
        incremental_active_start: str,
        incremental_active_end: str,
        incremental_canary_telegram_ids: list[int],
        incremental_monthly_budget_usd: float | None,
    ) -> Settings:
        if zillow_enabled and load_apify_token(str(self.store.root / ".env")) is None:
            raise ValueError("APIFY_TOKEN is required before Zillow can be enabled")
        if incremental_alerts_enabled:
            if not zillow_enabled:
                raise ValueError("enable Zillow before enabling incremental alerts")
            if not self.store.config_v2_status()["ready"]:
                raise ValueError("config migration is required before incremental alerts can run")
            if not self.store.alert_migration_status().ready:
                raise ValueError("alert-ledger migration is required before incremental alerts can run")
        allowlist_ids = {person.telegram_id for person in self.store.load_allowlist().people}
        unknown_canaries = set(incremental_canary_telegram_ids) - allowlist_ids
        if unknown_canaries:
            raise ValueError(
                "incremental canaries must already be on the allowlist: "
                + ", ".join(map(str, sorted(unknown_canaries)))
            )

        with self.store.edit_settings() as settings:
            self._guard_zillow_first_enable(settings, zillow_enabled, zillow_terms_confirmed)
            source_budgets = dict(settings.source_daily_request_budgets)
            source_budgets["zillow"] = zillow_daily_request_budget
            candidate = Settings.model_validate(
                {
                    **settings.model_dump(mode="python"),
                    "incremental_alerts_enabled": incremental_alerts_enabled,
                    "zillow_enabled": zillow_enabled,
                    "zillow_actor": zillow_actor,
                    "zillow_incremental_interval_minutes": (
                        zillow_incremental_interval_minutes
                    ),
                    "source_daily_request_budgets": source_budgets,
                    "zillow_results_limit": zillow_results_limit,
                    "zillow_max_charge_usd": zillow_max_charge_usd,
                    "incremental_active_start": incremental_active_start,
                    "incremental_active_end": incremental_active_end,
                    "incremental_canary_telegram_ids": incremental_canary_telegram_ids,
                    "incremental_monthly_budget_usd": incremental_monthly_budget_usd,
                }
            )
            for field in (
                "incremental_alerts_enabled",
                "zillow_enabled",
                "zillow_actor",
                "zillow_incremental_interval_minutes",
                "source_daily_request_budgets",
                "zillow_results_limit",
                "zillow_max_charge_usd",
                "incremental_active_start",
                "incremental_active_end",
                "incremental_canary_telegram_ids",
                "incremental_monthly_budget_usd",
            ):
                setattr(settings, field, getattr(candidate, field))
        return self.store.load_settings()

    @staticmethod
    def _guard_zillow_first_enable(settings, zillow_enabled: bool, terms_confirmed: bool) -> None:
        """Require an explicit terms confirmation the first time Zillow is turned on."""
        if zillow_enabled and not settings.zillow_enabled and not terms_confirmed:
            raise ValueError("confirm the Zillow managed-scraper warning before enabling it")

    def update_run_settings(
        self,
        *,
        live_push_enabled: bool,
        scrape_time: str,
        scrape_timezone: str,
        global_daily_request_budget: int,
        per_source_daily_request_budget: int,
        operator_alert_telegram_id: int | None,
        zillow_enabled: bool,
        zillow_terms_confirmed: bool,
        zillow_actor: str,
        zillow_daily_request_budget: int,
        zillow_results_limit: int,
        zillow_max_charge_usd: float,
        zillow_timeout_seconds: int,
    ) -> Settings:
        """Edit every daily-run global setting from the dashboard Config screen.

        Same safe shape as ``update_incremental_settings``: pre-flight checks
        raise before the lock; inside, a whole-object candidate re-validates
        (so a bad field — or an incoherent on-disk incremental config — raises
        with NO write); then the validated fields are copied back.
        """
        if zillow_enabled and load_apify_token(str(self.store.root / ".env")) is None:
            raise ValueError("APIFY_TOKEN is required before Zillow can be enabled")
        with self.store.edit_settings() as settings:
            self._guard_zillow_first_enable(settings, zillow_enabled, zillow_terms_confirmed)
            source_budgets = dict(settings.source_daily_request_budgets)
            source_budgets["zillow"] = zillow_daily_request_budget
            candidate = Settings.model_validate(
                {
                    **settings.model_dump(mode="python"),
                    "live_push_enabled": live_push_enabled,
                    "scrape_time": scrape_time,
                    "scrape_timezone": scrape_timezone,
                    "global_daily_request_budget": global_daily_request_budget,
                    "per_source_daily_request_budget": per_source_daily_request_budget,
                    "operator_alert_telegram_id": operator_alert_telegram_id,
                    "zillow_enabled": zillow_enabled,
                    "zillow_actor": zillow_actor,
                    "source_daily_request_budgets": source_budgets,
                    "zillow_results_limit": zillow_results_limit,
                    "zillow_max_charge_usd": zillow_max_charge_usd,
                    "zillow_timeout_seconds": zillow_timeout_seconds,
                }
            )
            for field in (
                "live_push_enabled",
                "scrape_time",
                "scrape_timezone",
                "global_daily_request_budget",
                "per_source_daily_request_budget",
                "operator_alert_telegram_id",
                "zillow_enabled",
                "zillow_actor",
                "source_daily_request_budgets",
                "zillow_results_limit",
                "zillow_max_charge_usd",
                "zillow_timeout_seconds",
            ):
                setattr(settings, field, getattr(candidate, field))
        return self.store.load_settings()

    def person_pushes_before_scrape(self, telegram_id: int) -> bool:
        """True when this person's push time is at/before the scrape gate today."""
        person = self.store.load_allowlist().get(telegram_id)
        if person is None:
            return False
        return push_precedes_scrape(
            person.profile, self.store.load_settings(), ref_date=self._today()
        )

    def people_pushing_before_scrape(self) -> list[tuple[int, str]]:
        """Active people whose push time is at/before the scrape gate (empty-morning risk)."""
        settings = self.store.load_settings()
        today = self._today()
        return [
            (person.telegram_id, person.display_name)
            for person in self.store.load_allowlist().active_people()
            if push_precedes_scrape(person.profile, settings, ref_date=today)
        ]

    def set_incremental_enabled(self, enabled: bool) -> Settings:
        current = self.store.load_settings()
        if enabled:
            if not current.zillow_enabled:
                raise ValueError("enable Zillow before resuming incremental collection")
            if load_apify_token(str(self.store.root / ".env")) is None:
                raise ValueError("APIFY_TOKEN is required before resuming")
            if not self.store.config_v2_status()["ready"]:
                raise ValueError("config migration is required before resuming")
            if not self.store.alert_migration_status().ready:
                raise ValueError("alert-ledger migration is required before resuming")
        with self.store.edit_settings() as settings:
            settings.incremental_alerts_enabled = enabled
        return self.store.load_settings()

    # ------------------------------------------------------------------ status
    def settings(self) -> Settings:
        return self.store.load_settings()

    # ---------------------------------------------------------- manual testing
    def prepare_test_push(self, telegram_id: int) -> TestPushPlan:
        """Validate the bot and recipient without sending, then build the payload."""
        if not self.store.load_settings().live_push_enabled:
            raise TestPushBlocked(
                "live delivery is disabled in Config; enable it before a test push"
            )
        plan = prepare_test_push(self.store, telegram_id)
        env_path = self.store.root / ".env"
        sender = build_sender(str(env_path))
        if sender is None:
            raise TestPushBlocked("no usable Telegram bot token is configured")
        try:
            sender.whoami()
        except TelegramSendError as exc:
            if exc.terminal and "unauthorized" in str(exc).lower():
                raise TestPushBlocked(
                    "Telegram bot token is invalid or revoked (401 Unauthorized). "
                    f"Replace TELEGRAM_BOT_TOKEN in {env_path} with the current "
                    "token from @BotFather, then retry."
                ) from None
            raise TestPushBlocked(
                f"Telegram bot authentication could not be verified: {exc}"
            ) from None
        try:
            sender.get_chat(telegram_id)
        except TelegramSendError as exc:
            if exc.terminal:
                raise TestPushBlocked(
                    f"Telegram cannot reach {plan.display_name} ({telegram_id}): {exc}. "
                    "That account must open this bot and press Start (/start), then retry."
                ) from None
            raise TestPushBlocked(
                f"Telegram recipient reachability could not be verified: {exc}"
            ) from None
        return plan

    def send_test_push(self, plan: TestPushPlan) -> TestPushResult:
        """Execute a confirmed plan and return its verified Telegram receipt."""
        try:
            sender = build_sender(str(self.store.root / ".env"))
        except Exception:
            # The service records the confirmed, blocked attempt even if the
            # credential file became unreadable after the confirmation modal.
            sender = None
        return deliver_test_push(self.store, plan, sender=sender)

    def latest_run(self) -> Optional[dict]:
        return self.store.latest_run_log()

    def _today(self) -> date:
        # Daily cache and quota follow the configured scrape clock's calendar,
        # not UTC. Otherwise New York's day rolls over four hours too early in
        # summer and the dashboard reports the wrong cache and spend.
        return scrape_day(self.store.load_settings(), datetime.now(timezone.utc))

    def quota_today(self) -> tuple[int, int]:
        settings = self.store.load_settings()
        used = self.store.quota_total(self._today())
        return used, settings.global_daily_request_budget

    def source_status_today(self) -> list[dict]:
        """Return real per-source config, quota, cache, and last-run state."""
        today = self._today()
        settings = self.store.load_settings()
        env_path = str(self.store.root / ".env")
        credentials = {
            "zillow": load_apify_token(env_path) is not None,
        }
        latest = self.store.latest_run_log() or {}
        fetch = latest.get("fetch") if isinstance(latest, dict) else {}
        source_runs = fetch.get("sources") if isinstance(fetch, dict) else None
        if not isinstance(source_runs, list):
            source_runs = [fetch] if isinstance(fetch, dict) and fetch.get("source") else []
        by_source = {
            str(item.get("source")): item
            for item in source_runs
            if isinstance(item, dict) and item.get("source")
        }

        rows: list[dict] = []
        for source in KNOWN_SOURCES:
            cached_report = load_daily_cached(
                self.store, source, now_utc=datetime.now(timezone.utc)
            )
            budget = settings.source_request_budget(source)
            enabled = credentials[source] and budget > 0
            readiness = "ready" if enabled else "disabled"
            if source == "zillow":
                enabled = settings.zillow_enabled and credentials[source] and budget > 0
                if not settings.zillow_enabled:
                    readiness = "disabled"
                elif not credentials[source]:
                    readiness = "needs token"
                elif budget <= 0 or settings.zillow_results_limit <= 0:
                    readiness = "budget 0"
                else:
                    readiness = "ready"
            elif not credentials[source]:
                readiness = "needs key"
            elif budget <= 0:
                readiness = "budget 0"

            run = by_source.get(source) or {}
            if run.get("errors"):
                health = "error"
            elif run.get("from_cache"):
                health = "cached"
            elif run.get("fetched"):
                health = "ok"
            elif run.get("warnings"):
                health = "waiting"
            else:
                health = "not run"
            rows.append(
                {
                    "source": source,
                    "enabled": enabled,
                    "readiness": readiness,
                    "used": self.store.quota_used(today, source),
                    "budget": budget,
                    "cached": cached_report.usable,
                    "records": run.get("records", len(cached_report.records)),
                    "health": health,
                }
            )
        return rows

    def rejected_today(self) -> int:
        return self.store.rejected_count(self._today())

    def incremental_status(self) -> dict:
        config = self.store.config_v2_status()
        ledger = self.store.alert_migration_status()
        settings = self.store.load_settings()
        token_ready = load_apify_token(str(self.store.root / ".env")) is not None
        status = {
            "config": config,
            "ledger": ledger.as_dict(),
            "enabled": settings.incremental_alerts_enabled,
            "zillow_enabled": settings.zillow_enabled,
            "token_ready": token_ready,
            "actor": settings.zillow_actor,
            "interval_minutes": settings.zillow_incremental_interval_minutes,
            "daily_budget": settings.source_request_budget("zillow"),
            "used_today": self.store.quota_used(self._today(), "zillow"),
            "results_limit": settings.zillow_results_limit,
            "max_charge_usd": settings.zillow_max_charge_usd,
            "active_window": (
                f"{settings.incremental_active_start}-{settings.incremental_active_end} "
                f"{settings.scrape_timezone}"
            ),
            "canary_ids": settings.incremental_canary_telegram_ids,
            "health": None,
        }
        status["remaining_today"] = max(
            0, status["daily_budget"] - status["used_today"]
        )
        if ledger.ready:
            health = self.store.event_store().health_snapshot()
            last_success = health.get("last_success_at")
            if last_success:
                parsed = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                age_minutes = max(
                    0, int((datetime.now(timezone.utc) - parsed).total_seconds() // 60)
                )
                health["last_success_age_minutes"] = age_minutes
                health["stale"] = age_minutes > (
                    settings.zillow_incremental_interval_minutes * 2
                )
            else:
                health["last_success_age_minutes"] = None
                health["stale"] = bool(health["active_scopes"])
            for scope in health["scopes"]:
                last_scope_success = scope.get("last_success_at")
                if last_scope_success:
                    parsed = datetime.fromisoformat(
                        str(last_scope_success).replace("Z", "+00:00")
                    )
                    age = max(
                        0,
                        int(
                            (datetime.now(timezone.utc) - parsed).total_seconds()
                            // 60
                        ),
                    )
                    scope["age_minutes"] = age
                    scope["stale"] = age > (
                        settings.zillow_incremental_interval_minutes * 2
                    )
                else:
                    scope["age_minutes"] = None
                    scope["stale"] = True
            status["health"] = health
            status["cost"] = incremental_cost_status(
                self.store, settings, now_utc=datetime.now(timezone.utc)
            )
            status["rollout"] = incremental_rollout_readiness(
                self.store,
                settings,
                now_utc=datetime.now(timezone.utc),
                token_ready=token_ready,
                telegram_ready=build_sender(str(self.store.root / ".env")) is not None,
            )
        else:
            status["cost"] = None
            status["rollout"] = incremental_rollout_readiness(
                self.store,
                settings,
                now_utc=datetime.now(timezone.utc),
                token_ready=token_ready,
                telegram_ready=build_sender(str(self.store.root / ".env")) is not None,
            )
        return status

    def recipient_outbox_counts(self, telegram_id: int) -> dict[str, int]:
        if not self.store.alert_migration_status().ready:
            return {}
        return self.store.event_store().outbox_counts(telegram_id=telegram_id)

    def incremental_outbox(self):
        if not self.store.alert_migration_status().ready:
            return []
        return self.store.event_store().outbox_records()

    def retry_failed_outbox(self, outbox_id: int) -> None:
        if not self.store.alert_migration_status().ready:
            raise ValueError("alert-ledger migration is required")
        self.store.event_store().retry_failed_outbox(
            outbox_id, now_utc=datetime.now(timezone.utc)
        )

    def reset_baseline(self, query_id: str, *, reason: str) -> None:
        if not self.store.alert_migration_status().ready:
            raise ValueError("alert-ledger migration is required")
        self.store.event_store().reset_query_baseline(
            query_id, now_utc=datetime.now(timezone.utc), reason=reason
        )

    def run_shadow_cycle(self, *, now_utc: datetime | None = None) -> dict:
        """Run one real source cycle and shadow processing; never send Telegram."""
        settings = self.store.load_settings()
        token = load_apify_token(str(self.store.root / ".env"))
        if not token:
            raise ValueError("APIFY_TOKEN is required for a shadow cycle")
        if not settings.incremental_alerts_enabled or not settings.zillow_enabled:
            raise ValueError("incremental collection and Zillow must both be enabled")
        with self.store.try_run_lock() as acquired:
            if not acquired:
                raise ValueError("another rental workflow is already running")
            now_utc = now_utc or datetime.now(timezone.utc)
            report = IncrementalCollector(
                self.store,
                settings=settings,
                adapter=ZillowRentalAdapter(
                    token=token,
                    actor=settings.zillow_actor,
                    results_limit=settings.zillow_results_limit,
                    timeout=settings.zillow_timeout_seconds,
                    max_charge_usd=settings.zillow_max_charge_usd,
                ),
                client=ApifyClient(
                    token=token, timeout=settings.zillow_timeout_seconds
                ),
            ).cycle(now_utc=now_utc)
            processing = process_incremental_report(
                self.store, report, now_utc=now_utc
            )
        summary = report.summary()
        summary["processing"] = processing.summary()
        return summary

    def preview(self, listings, *, now_utc: Optional[datetime] = None) -> PipelineResult:
        return plan_pushes(
            self.store, listings, now_utc=now_utc or datetime.now(timezone.utc)
        )


__all__ = ["TuiController", "DuplicateError", "NotFoundError", "StoreError"]
