"""On-demand (hot) VinAudit behavior in resolve_vin.

Rules verified:
  * VinAudit is queried HOT, per VIN, and its price is cached on the Vehicle.
  * The same VIN within VINAUDIT_TTL_DAYS is served from cache (no API call).
  * A VIN older than the TTL is re-queried only when the user asks (or force).
  * A definitive not-found is cached (negative) so VinAudit isn't re-asked; the
    request falls back to the background scrapers.
  * A transient failure keeps any prior VinAudit price and is retryable.
  * A VinAudit-priced VIN is never overwritten by the background model scrape.
  * Prewarm (allow_vinaudit=False) never spends VinAudit quota.
"""
from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from scraper import services
from scraper.models import ScrapeJob, ScraperSource, Vehicle, VehicleModel
from scraper.providers.base import BlockedError, ScrapedVehicle, VehicleNotFound
from scraper.vin_decoder import DecodedVin

VIN = "1NXBR32E85Z505904"

# What the fake VinAudit provider returns (or raises) when .scrape() is called.
_SCRAPE: dict = {"result": None}
_CALLS: list = []


def _va_result(est=7044, low=5768, high=8320):
    return ScrapedVehicle(
        vin=VIN, make="Toyota", model="Corolla LE", year=2005,
        estimated_price=Decimal(str(est)),
        price_low=Decimal(str(low)), price_high=Decimal(str(high)),
        price_kind="vinaudit_market", currency="USD",
        source_url="https://marketvalue.vinaudit.com/v2/marketvalue?vin=%s&format=json" % VIN,
        raw_data={"provider": "vinaudit", "mean": est},
    )


class _FakeVinAudit:
    def __init__(self, source):
        self.source = source

    def scrape(self, vin):
        _CALLS.append(vin)
        r = _SCRAPE["result"]
        if isinstance(r, Exception):
            raise r
        return r


def _fake_gpc(key):
    return _FakeVinAudit  # only the vinaudit key is invoked from resolve_vin


_DECODED = DecodedVin(vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE")


@override_settings(VINAUDIT_API_KEY="TESTKEY", VINAUDIT_TTL_DAYS=30)
class VinAuditHotResolveTest(TestCase):
    def setUp(self):
        _SCRAPE["result"] = None
        _CALLS.clear()
        self.vinaudit = ScraperSource.objects.create(
            name="VinAudit Market Value", slug="vinaudit", provider_key="vinaudit",
            base_url="https://marketvalue.vinaudit.com",
            vin_path_template="/v2/marketvalue?vin={vin}", model_path_template="",
            priority=5, is_active=True,
        )

    def _resolve(self, **kw):
        with patch.object(services, "decode_vin", return_value=_DECODED), \
             patch.object(services, "get_provider_class", _fake_gpc):
            return services.resolve_vin(VIN, **kw)

    def test_first_lookup_calls_vinaudit_and_caches(self):
        _SCRAPE["result"] = _va_result()
        vehicle, state, job = self._resolve()

        self.assertEqual(state, services.STATUS_READY)
        self.assertIsNone(job)  # served hot, nothing enqueued
        self.assertEqual(_CALLS, [VIN])  # VinAudit was queried once
        self.assertEqual(vehicle.estimated_price, Decimal("7044.00"))
        self.assertEqual(vehicle.price_low, Decimal("5768.00"))
        self.assertEqual(vehicle.price_high, Decimal("8320.00"))
        self.assertEqual(vehicle.price_kind, "vinaudit_market")
        self.assertIsNotNone(vehicle.vinaudit_priced_at)
        self.assertIsNotNone(vehicle.vinaudit_checked_at)

    def test_second_lookup_within_ttl_uses_cache(self):
        _SCRAPE["result"] = _va_result()
        self._resolve()          # first: 1 call
        self._resolve()          # second: within TTL -> no call
        self.assertEqual(_CALLS, [VIN])  # still just one query total

    def test_lookup_after_ttl_requeries(self):
        _SCRAPE["result"] = _va_result()
        self._resolve()
        # Age the last check beyond the TTL.
        Vehicle.objects.filter(vin=VIN).update(
            vinaudit_checked_at=timezone.now() - timedelta(days=31),
            vinaudit_priced_at=timezone.now() - timedelta(days=31),
        )
        _SCRAPE["result"] = _va_result(est=6800, low=5600, high=8000)
        vehicle, state, _ = self._resolve()

        self.assertEqual(len(_CALLS), 2)  # re-queried
        self.assertEqual(vehicle.estimated_price, Decimal("6800.00"))

    def test_force_requeries_even_if_fresh(self):
        _SCRAPE["result"] = _va_result()
        self._resolve()
        self._resolve(force=True)
        self.assertEqual(len(_CALLS), 2)

    def test_not_found_caches_negative_and_falls_back(self):
        _SCRAPE["result"] = VehicleNotFound("no data")
        vehicle, state, job = self._resolve()

        self.assertEqual(state, services.STATUS_PROCESSING)  # fell back to scrapers
        self.assertIsNotNone(job)
        self.assertEqual(ScrapeJob.objects.count(), 1)
        # Negative cached: checked_at set, but not priced (not owned).
        vehicle.refresh_from_db()
        self.assertIsNotNone(vehicle.vinaudit_checked_at)
        self.assertIsNone(vehicle.vinaudit_priced_at)
        # A second lookup within TTL does NOT re-query VinAudit.
        self._resolve()
        self.assertEqual(_CALLS, [VIN])

    def test_transient_failure_keeps_prior_vinaudit_price(self):
        # Seed a VIN already priced by VinAudit, with an expired check.
        Vehicle.objects.create(
            vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("7044.00"), price_low=Decimal("5768.00"),
            price_high=Decimal("8320.00"), price_kind="vinaudit_market",
            source=self.vinaudit,
            vinaudit_priced_at=timezone.now() - timedelta(days=40),
            vinaudit_checked_at=timezone.now() - timedelta(days=40),
        )
        _SCRAPE["result"] = BlockedError("rate limit")
        vehicle, state, job = self._resolve()

        self.assertEqual(state, services.STATUS_READY)  # kept stale VinAudit price
        self.assertIsNone(job)
        self.assertEqual(vehicle.estimated_price, Decimal("7044.00"))
        self.assertEqual(len(_CALLS), 1)  # tried once
        # Not re-cached (so a later request can retry): check stays old.
        self.assertLess(
            vehicle.vinaudit_checked_at, timezone.now() - timedelta(days=39)
        )

    def test_prewarm_never_calls_vinaudit(self):
        _SCRAPE["result"] = _va_result()
        vehicle, state, job = self._resolve(allow_vinaudit=False)
        self.assertEqual(_CALLS, [])  # VinAudit untouched
        self.assertEqual(state, services.STATUS_PROCESSING)  # model flow

    def test_prewarm_does_not_overwrite_vinaudit_price(self):
        """#1: prewarm (allow_vinaudit=False) hits the model flow, which must NOT
        overwrite a VIN already priced by VinAudit even with a fresh model row."""
        edm = ScraperSource.objects.create(
            name="Edmunds", slug="edmunds", provider_key="edmunds",
            model_path_template="/{make}/{model}/{year}/", priority=10,
        )
        VehicleModel.objects.create(
            make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("6000.00"), price_low=Decimal("5000.00"),
            price_high=Decimal("7000.00"), price_kind="edmunds_market", source=edm,
        )
        Vehicle.objects.create(
            vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("7044.00"), price_kind="vinaudit_market",
            source=self.vinaudit, vinaudit_priced_at=timezone.now(),
            vinaudit_checked_at=timezone.now(),
        )
        vehicle, state, job = self._resolve(allow_vinaudit=False)
        self.assertEqual(vehicle.estimated_price, Decimal("7044.00"))
        self.assertEqual(vehicle.price_kind, "vinaudit_market")
        self.assertIsNone(vehicle.vehicle_model_id)  # _link_model no-op'd

    @override_settings(VINAUDIT_API_KEY="")
    def test_no_key_skips_source_query(self):
        """#5: with no API key, the ScraperSource query is skipped entirely."""
        with patch.object(services, "_vinaudit_source") as src:
            _vehicle, state, _job = self._resolve()
        src.assert_not_called()
        self.assertEqual(state, services.STATUS_PROCESSING)  # went to model flow

    def test_vinaudit_only_success_returns_price(self):
        _SCRAPE["result"] = _va_result()
        vehicle, state, job = self._resolve(vinaudit_only=True)
        self.assertEqual(state, services.STATUS_READY)
        self.assertEqual(vehicle.estimated_price, Decimal("7044.00"))

    def test_vinaudit_only_served_from_cache_no_error(self):
        """A VIN already priced by VinAudit within TTL: the paid button returns the
        CACHED data (ready), NOT an error, and makes NO new API call (no re-charge)."""
        _SCRAPE["result"] = _va_result()
        self._resolve(vinaudit_only=True)              # 1st: prices + caches (1 call)
        vehicle, state, job = self._resolve(vinaudit_only=True)  # 2nd: within TTL
        self.assertEqual(state, services.STATUS_READY)  # data, not error
        self.assertEqual(vehicle.estimated_price, Decimal("7044.00"))
        self.assertIsNone(job)
        self.assertEqual(len(_CALLS), 1)                # served from cache, no 2nd call

    def test_vinaudit_only_keeps_stale_cache_when_requery_fails(self):
        """A cached VIN past the TTL whose re-query fails still returns the cached
        VinAudit price (not an error)."""
        _SCRAPE["result"] = _va_result()
        self._resolve(vinaudit_only=True)  # price + cache
        Vehicle.objects.filter(vin=VIN).update(
            vinaudit_priced_at=timezone.now() - timedelta(days=40),
            vinaudit_checked_at=timezone.now() - timedelta(days=40),
        )
        _SCRAPE["result"] = BlockedError("rate limit")  # re-query fails
        vehicle, state, job = self._resolve(vinaudit_only=True)
        self.assertEqual(state, services.STATUS_READY)  # kept cached price, no error
        self.assertEqual(vehicle.estimated_price, Decimal("7044.00"))

    def test_vinaudit_only_errors_on_no_data(self):
        """Paid button: VinAudit not-found -> error, NO free fallback / no job."""
        _SCRAPE["result"] = VehicleNotFound("no data")
        with self.assertRaises(services.VinAuditLookupError) as cm:
            self._resolve(vinaudit_only=True)
        self.assertEqual(cm.exception.reason, "no_data")
        self.assertEqual(ScrapeJob.objects.count(), 0)  # nothing enqueued

    def test_vinaudit_only_errors_on_unavailable(self):
        """Paid button: VinAudit down (rate limit/etc.) -> error, no fallback."""
        _SCRAPE["result"] = BlockedError("rate limit")
        with self.assertRaises(services.VinAuditLookupError) as cm:
            self._resolve(vinaudit_only=True)
        self.assertEqual(cm.exception.reason, "unavailable")
        self.assertEqual(ScrapeJob.objects.count(), 0)


class VinAuditModelScrapeProtectionTest(TestCase):
    """A background model scrape must not overwrite a VinAudit-priced VIN."""

    def test_apply_model_skips_vinaudit_owned_vin(self):
        vinaudit = ScraperSource.objects.create(
            name="VinAudit", slug="vinaudit", provider_key="vinaudit",
            model_path_template="", priority=5,
        )
        edmunds = ScraperSource.objects.create(
            name="Edmunds", slug="edmunds", provider_key="edmunds",
            model_path_template="/{make}/{model}/{year}/", priority=10,
        )
        veh = Vehicle.objects.create(
            vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("7044.00"), price_kind="vinaudit_market",
            source=vinaudit, vinaudit_priced_at=timezone.now(),
        )
        # A NORMAL (non-VinAudit) VIN of the same model — must be updated.
        normal = Vehicle.objects.create(
            vin="2HGES16575H000000", make="Toyota", model="Corolla", year=2014, trim="LE",
        )
        vm = VehicleModel.objects.create(
            make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("6000.00"), price_low=Decimal("5000.00"),
            price_high=Decimal("7000.00"), price_kind="edmunds_market",
            source=edmunds,
        )
        updated = services.apply_model_to_vehicles(vm)

        self.assertEqual(updated, 1)  # only the normal VIN; VinAudit one excluded
        veh.refresh_from_db()
        self.assertEqual(veh.estimated_price, Decimal("7044.00"))
        self.assertEqual(veh.price_kind, "vinaudit_market")
        normal.refresh_from_db()
        self.assertEqual(normal.price_low, Decimal("5000.00"))  # range copied (#7/#4)
        self.assertEqual(normal.price_high, Decimal("7000.00"))
        self.assertEqual(normal.price_kind, "edmunds_market")

    def test_link_model_skips_vinaudit_owned(self):
        """#1: _link_model itself is the choke point — it no-ops a VinAudit VIN."""
        edmunds = ScraperSource.objects.create(
            name="Edmunds", slug="edmunds", provider_key="edmunds",
            model_path_template="/{make}/{model}/{year}/", priority=10,
        )
        vinaudit = ScraperSource.objects.create(
            name="VinAudit", slug="vinaudit", provider_key="vinaudit",
            model_path_template="", priority=5,
        )
        vm = VehicleModel.objects.create(
            make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("6000.00"), price_low=Decimal("5000.00"),
            price_high=Decimal("7000.00"), price_kind="edmunds_market", source=edmunds,
        )
        veh = Vehicle.objects.create(
            vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("7044.00"), price_kind="vinaudit_market",
            source=vinaudit, vinaudit_priced_at=timezone.now(),
        )
        services._link_model(veh, vm)
        veh.refresh_from_db()
        self.assertEqual(veh.estimated_price, Decimal("7044.00"))
        self.assertEqual(veh.price_kind, "vinaudit_market")
        self.assertIsNone(veh.vehicle_model_id)

    def test_worker_never_queries_vinaudit(self):
        """scrape_model_data (the worker path) excludes VinAudit entirely, because
        the VinAudit source has an empty model_path_template."""
        ScraperSource.objects.create(
            name="VinAudit", slug="vinaudit", provider_key="vinaudit",
            model_path_template="", priority=5, is_active=True,
        )
        ScraperSource.objects.create(
            name="Edmunds", slug="edmunds", provider_key="edmunds",
            model_path_template="/{make}/{model}/{year}/", priority=10, is_active=True,
        )
        instantiated: list = []

        def _recording_gpc(key):
            instantiated.append(key)

            class _P:
                def __init__(self, source):
                    pass

                def scrape_model(self, *a, **k):
                    raise VehicleNotFound("none")

            return _P

        with patch.object(services, "get_provider_class", _recording_gpc):
            with self.assertRaises(services.AllSourcesFailed):
                services.scrape_model_data("Toyota", "Corolla", 2014, "LE")

        self.assertNotIn("vinaudit", instantiated)  # never touched by the worker
        self.assertIn("edmunds", instantiated)


class ForceGateTest(TestCase):
    """#2: force (which triggers a paid VinAudit re-query) is honored only for a
    logged-in staff session, so an unauthenticated caller can't drain paid quota."""

    def setUp(self):
        self.client = APIClient()
        self.vehicle = Vehicle.objects.create(
            vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE",
        )

    @patch("scraper.views.resolve_vin")
    def test_anonymous_force_is_ignored(self, mock_resolve):
        mock_resolve.return_value = (self.vehicle, services.STATUS_READY, None)
        r = self.client.post(
            "/api/vehicles/lookup/", {"vin": VIN, "force": True}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(mock_resolve.call_args.kwargs["force"])  # force dropped

    @patch("scraper.views.resolve_vin")
    def test_staff_force_is_honored(self, mock_resolve):
        mock_resolve.return_value = (self.vehicle, services.STATUS_READY, None)
        staff = get_user_model().objects.create_user(
            "op", password="x", is_staff=True
        )
        self.client.force_login(staff)
        r = self.client.post(
            "/api/vehicles/lookup/", {"vin": VIN, "force": True}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(mock_resolve.call_args.kwargs["force"])  # force honored

    @patch("scraper.views.resolve_vin")
    def test_normal_lookup_does_not_use_vinaudit(self, mock_resolve):
        mock_resolve.return_value = (self.vehicle, services.STATUS_READY, None)
        r = self.client.post("/api/vehicles/lookup/", {"vin": VIN}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(mock_resolve.call_args.kwargs["allow_vinaudit"])  # free search

    @patch("scraper.views.resolve_vin")
    def test_use_vinaudit_opts_in(self, mock_resolve):
        mock_resolve.return_value = (self.vehicle, services.STATUS_READY, None)
        r = self.client.post(
            "/api/vehicles/lookup/", {"vin": VIN, "use_vinaudit": True}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(mock_resolve.call_args.kwargs["allow_vinaudit"])  # opted in
        self.assertTrue(mock_resolve.call_args.kwargs["vinaudit_only"])  # no free fallback

    @patch("scraper.views.resolve_vin")
    def test_paid_lookup_no_data_returns_404(self, mock_resolve):
        mock_resolve.side_effect = services.VinAuditLookupError("no_data")
        r = self.client.post(
            "/api/vehicles/lookup/", {"vin": VIN, "use_vinaudit": True}, format="json"
        )
        self.assertEqual(r.status_code, 404)
        self.assertIn("VinAudit", r.json()["detail"])

    @patch("scraper.views.resolve_vin")
    def test_paid_lookup_unavailable_returns_503(self, mock_resolve):
        mock_resolve.side_effect = services.VinAuditLookupError("unavailable")
        r = self.client.post(
            "/api/vehicles/lookup/", {"vin": VIN, "use_vinaudit": True}, format="json"
        )
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "error")


class ThrottleTest(TestCase):
    """The paid VinAudit lookup is uncapped; free lookups keep the anon cap."""

    def _req(self, body):
        from rest_framework.parsers import JSONParser
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        raw = APIRequestFactory().post("/api/vehicles/lookup/", body, format="json")
        raw.META["REMOTE_ADDR"] = "203.0.113.7"
        return Request(raw, parsers=[JSONParser()])

    def test_paid_lookup_is_not_capped(self):
        from django.core.cache import cache
        from scraper.views import FreeLookupThrottle

        cache.clear()
        req = self._req({"vin": VIN, "use_vinaudit": True})
        # Far more than any anon rate — all allowed (no cap on the paid lookup).
        self.assertTrue(all(
            FreeLookupThrottle().allow_request(req, None) for _ in range(200)
        ))

    def test_free_lookup_is_capped(self):
        from django.core.cache import cache
        from rest_framework.settings import api_settings
        from scraper.views import FreeLookupThrottle

        cache.clear()
        limit = int(api_settings.DEFAULT_THROTTLE_RATES["anon"].split("/")[0])
        req = self._req({"vin": VIN})  # free lookup
        allowed = sum(
            1 for _ in range(limit + 5)
            if FreeLookupThrottle().allow_request(req, None)
        )
        self.assertEqual(allowed, limit)  # free lookups still hit the anon cap

    def test_string_false_does_not_bypass_cap(self):
        """#2: use_vinaudit as the string "false" is a FREE lookup (serializer parses
        it to False) and must still be capped — not read as truthy to skip the cap."""
        from django.core.cache import cache
        from rest_framework.settings import api_settings
        from scraper.views import FreeLookupThrottle

        cache.clear()
        limit = int(api_settings.DEFAULT_THROTTLE_RATES["anon"].split("/")[0])
        req = self._req({"vin": VIN, "use_vinaudit": "false"})
        allowed = sum(
            1 for _ in range(limit + 5)
            if FreeLookupThrottle().allow_request(req, None)
        )
        self.assertEqual(allowed, limit)  # capped, NOT bypassed


class LinkModelGuardTest(TestCase):
    """#5/#13: _link_model is an atomic, write-only-when-changed choke point."""

    def _vm(self):
        edm = ScraperSource.objects.create(
            name="Edmunds", slug="edmunds", provider_key="edmunds", model_path_template="/x/",
        )
        return VehicleModel.objects.create(
            make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("6000.00"), price_low=Decimal("5000.00"),
            price_high=Decimal("7000.00"), price_kind="edmunds_market", source=edm,
        )

    def test_writes_then_skips_unchanged(self):
        vm = self._vm()
        veh = Vehicle.objects.create(vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE")
        self.assertTrue(services._link_model(veh, vm))          # first: writes
        self.assertEqual(veh.estimated_price, Decimal("6000.00"))
        self.assertFalse(services._link_model(veh, vm))         # #13: unchanged -> no write

    def test_atomic_guard_vs_concurrent_vinaudit(self):
        """#5: a VinAudit valuation committed in the DB (stale in-memory guard) is not
        overwritten — the conditional UPDATE matches 0 rows."""
        vm = self._vm()
        veh = Vehicle.objects.create(
            vin=VIN, make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("9999.00"), price_kind="vinaudit_market",
        )
        # Concurrent VinAudit write lands in the DB; our in-memory instance is stale.
        Vehicle.objects.filter(pk=veh.pk).update(vinaudit_priced_at=timezone.now())
        veh.vinaudit_priced_at = None  # stale read: guard would pass in-memory

        wrote = services._link_model(veh, vm)
        self.assertFalse(wrote)  # conditional UPDATE (WHERE vinaudit_priced_at IS NULL) = 0 rows
        veh.refresh_from_db()
        self.assertEqual(veh.estimated_price, Decimal("9999.00"))  # VinAudit price preserved
        self.assertEqual(veh.price_kind, "vinaudit_market")


class WebhookPayloadTest(TestCase):
    """#10: the webhook reports the VIN's OWN price (VinAudit when set), not the model."""

    def test_prefers_vehicle_own_price(self):
        from scraper import webhooks

        veh = Vehicle.objects.create(
            vin=VIN, make="Toyota", model="Corolla", year=2014,
            estimated_price=Decimal("7044.00"), price_kind="vinaudit_market",
        )
        vm = VehicleModel.objects.create(
            make="Toyota", model="Corolla", year=2014, trim="",
            estimated_price=Decimal("6000.00"), price_kind="edmunds_market",
        )
        job = ScrapeJob.objects.create(
            make="Toyota", model="Corolla", year=2014, vin=VIN, status="done",
        )
        payload = webhooks._payload(job, vm, veh)
        self.assertEqual(payload["estimated_price"], "7044.00")  # VinAudit, not vm's 6000
        self.assertEqual(payload["price_kind"], "vinaudit_market")


class VinAuditEnabledFlagTest(TestCase):
    """services.vinaudit_enabled() gates the paid button (key AND active source)."""

    def test_disabled_without_key(self):
        self.assertFalse(services.vinaudit_enabled())

    @override_settings(VINAUDIT_API_KEY="TESTKEY")
    def test_enabled_with_key_and_active_source(self):
        ScraperSource.objects.create(
            name="VinAudit", slug="vinaudit", provider_key="vinaudit",
            model_path_template="", is_active=True,
        )
        self.assertTrue(services.vinaudit_enabled())

    @override_settings(VINAUDIT_API_KEY="TESTKEY")
    def test_disabled_when_source_inactive(self):
        ScraperSource.objects.create(
            name="VinAudit", slug="vinaudit", provider_key="vinaudit",
            model_path_template="", is_active=False,
        )
        self.assertFalse(services.vinaudit_enabled())


class VinAuditButtonVisibilityTest(TestCase):
    """The paid button (and its cost notice) render only when VinAudit is enabled."""

    def _html(self):
        return self.client.get("/", HTTP_HOST="localhost").content.decode()

    def test_button_hidden_when_disabled(self):
        html = self._html()  # no key -> disabled
        self.assertNotIn("Valorar con VinAudit", html)
        self.assertNotIn('id="vinauditBox"', html)

    @override_settings(
        VINAUDIT_API_KEY="TESTKEY",
        VINAUDIT_COST_NOTICE="Cada consulta cuesta $0.50 por uso.",
    )
    def test_button_and_notice_shown_when_enabled(self):
        ScraperSource.objects.create(
            name="VinAudit", slug="vinaudit", provider_key="vinaudit",
            model_path_template="", is_active=True,
        )
        html = self._html()
        self.assertIn("Valorar con VinAudit", html)
        self.assertIn('id="vinauditBox"', html)
        self.assertIn("Cada consulta cuesta $0.50 por uso.", html)


class BackfillMigrationTest(TestCase):
    """#4: the 0014 data migration copies the range onto pre-existing linked rows
    and leaves VinAudit-priced VINs untouched."""

    def test_backfill_fills_linked_and_skips_vinaudit(self):
        from django.apps import apps as global_apps

        edm = ScraperSource.objects.create(
            name="Edmunds", slug="edmunds", provider_key="edmunds",
            model_path_template="/x/", priority=10,
        )
        vm = VehicleModel.objects.create(
            make="Toyota", model="Corolla", year=2014, trim="LE",
            estimated_price=Decimal("6000.00"), price_low=Decimal("5000.00"),
            price_high=Decimal("7000.00"), price_kind="edmunds_market", source=edm,
        )
        # Pre-migration row: linked but no own range (columns NULL).
        v1 = Vehicle.objects.create(
            vin="1HGES16575H000000", make="Toyota", model="Corolla", year=2014,
            trim="LE", vehicle_model=vm,
        )
        # VinAudit-owned row: must be skipped.
        v2 = Vehicle.objects.create(
            vin="2HGES16575H000000", make="Toyota", model="Corolla", year=2014,
            trim="LE", vehicle_model=vm, estimated_price=Decimal("7044.00"),
            price_low=Decimal("5768.00"), price_high=Decimal("8320.00"),
            price_kind="vinaudit_market", vinaudit_priced_at=timezone.now(),
        )
        mig = importlib.import_module(
            "scraper.migrations.0014_backfill_vehicle_price_range"
        )
        mig.backfill_price_range(global_apps, None)

        v1.refresh_from_db()
        self.assertEqual(v1.price_low, Decimal("5000.00"))
        self.assertEqual(v1.price_high, Decimal("7000.00"))
        self.assertEqual(v1.price_kind, "edmunds_market")
        v2.refresh_from_db()  # VinAudit row untouched
        self.assertEqual(v2.price_low, Decimal("5768.00"))
        self.assertEqual(v2.price_kind, "vinaudit_market")
