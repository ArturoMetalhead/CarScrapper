"""Tests for the VinAudit Market Value provider.

Covers the request built (VIN query, mileage omitted for average), the
success->ScrapedVehicle mapping (price + range + keyless source_url), and the
error-code mapping onto the shared provider exceptions per the v2 docs.

The HTTP call (`_http_get`) is stubbed so no network is touched.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from scraper.providers.base import BlockedError, ScraperError, VehicleNotFound
from scraper.providers.vinaudit import VinAuditProvider

VIN = "2T1BURHE3EC020936"


class _FakeSource:
    """Minimal ScraperSource stand-in (the provider only reads `timeout`)."""

    name = "VinAudit Market Value"
    provider_key = "vinaudit"
    timeout = None


class _FakeResp:
    """Minimal requests.Response stand-in for _http_get."""

    def __init__(self, payload, url="https://marketvalue.vinaudit.com/v2/marketvalue"):
        self._payload = payload
        self.url = url

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


# A documented-shape success body (from VinAudit's v2 example).
SAMPLE = {
    "success": True,
    "vin": VIN,
    "id": "2014_toyota_corolla_s",
    "vehicle": "2014 Toyota Corolla S",
    "mileage": 75248,
    "count": 120,
    "mean": 7044,
    "stdev": 1276,
    "certainty": 99,
    "period": ["2015-06-27", "2015-07-16"],
    "type": "retail",
    "prices": {"average": 7044, "below": 5768, "above": 8320},
    "adjustments": {"mileage": {"adjustment": 0}},
}


def _provider():
    return VinAuditProvider(_FakeSource())


@override_settings(VINAUDIT_API_KEY="TESTKEY", VINAUDIT_COUNTRY="usa", VINAUDIT_MILEAGE="average")
class VinAuditRequestMappingTests(SimpleTestCase):
    def test_scrape_maps_prices_and_range(self):
        prov = _provider()
        with patch.object(prov, "_http_get", return_value=_FakeResp(SAMPLE)) as http:
            result = prov.scrape(VIN)

        self.assertEqual(result.vin, VIN)
        self.assertEqual(result.estimated_price, Decimal("7044.00"))
        self.assertEqual(result.price_low, Decimal("5768.00"))
        self.assertEqual(result.price_high, Decimal("8320.00"))
        self.assertEqual(result.price_kind, "vinaudit_market")
        self.assertEqual(result.currency, "USD")
        # VIN + key were sent; the source_url is keyless.
        _url, params = http.call_args.args
        self.assertEqual(params["vin"], VIN)
        self.assertEqual(params["key"], "TESTKEY")
        self.assertNotIn("TESTKEY", result.source_url)
        self.assertIn("vin=%s" % VIN, result.source_url)

    def test_parses_vehicle_title(self):
        prov = _provider()
        with patch.object(prov, "_http_get", return_value=_FakeResp(SAMPLE)):
            result = prov.scrape(VIN)
        self.assertEqual(result.year, 2014)
        self.assertEqual(result.make, "Toyota")
        self.assertEqual(result.model, "Corolla S")

    def test_mileage_omitted_for_average(self):
        """Per the v2 docs, average mileage means OMITTING the param (not 'average')."""
        prov = _provider()
        with patch.object(prov, "_http_get", return_value=_FakeResp(SAMPLE)) as http:
            prov.scrape(VIN)
        _url, params = http.call_args.args
        self.assertNotIn("mileage", params)

    @override_settings(VINAUDIT_MILEAGE="90000")
    def test_mileage_sent_when_numeric(self):
        prov = _provider()
        with patch.object(prov, "_http_get", return_value=_FakeResp(SAMPLE)) as http:
            prov.scrape(VIN)
        _url, params = http.call_args.args
        self.assertEqual(params["mileage"], "90000")

    @override_settings(VINAUDIT_COUNTRY="canada")
    def test_canada_uses_cad(self):
        prov = _provider()
        with patch.object(prov, "_http_get", return_value=_FakeResp(SAMPLE)):
            result = prov.scrape(VIN)
        self.assertEqual(result.currency, "CAD")

    def test_model_scrape_is_unsupported(self):
        """VinAudit is on-demand by VIN only — the model scraper path is refused."""
        with self.assertRaises(ScraperError):
            _provider().scrape_model("Toyota", "Corolla", 2014, "S")


class VinAuditErrorMappingTests(SimpleTestCase):
    """Documented v2 error codes -> provider exceptions."""

    def _check(self, code):
        return VinAuditProvider._check({"success": False, "error": code}, "x")

    def test_no_data_is_vehicle_not_found(self):
        with self.assertRaises(VehicleNotFound):
            self._check("no_data")

    def test_invalid_vehicle_is_vehicle_not_found(self):
        with self.assertRaises(VehicleNotFound):
            self._check("invalid_vehicle")

    def test_rate_limit_is_blocked(self):
        with self.assertRaises(BlockedError):
            self._check("rate_limit_exceeded")

    def test_internal_error_is_blocked_not_notfound(self):
        # Transient server issue must be retryable, NOT cached as a negative.
        with self.assertRaises(BlockedError):
            self._check("internal_error")

    def test_invalid_key_is_scraper_error(self):
        with self.assertRaises(ScraperError):
            self._check("invalid_key")

    def test_invalid_request_is_scraper_error_not_notfound(self):
        # A malformed request is our bug — retryable, NOT cached as a negative.
        with self.assertRaises(ScraperError):
            self._check("invalid_request")

    def test_unknown_code_is_retryable_scraper_error(self):
        with self.assertRaises(ScraperError):
            self._check("some_new_code")

    def test_undocumented_data_code_is_retryable_not_cached(self):
        # An undocumented code containing "data" (e.g. a transient "database_error")
        # must NOT map to VehicleNotFound (which the caller caches for 30 days).
        with self.assertRaises(ScraperError):
            self._check("database_error")
        with self.assertRaises(ScraperError):
            self._check("no_market_data")

    def test_legacy_invalid_vin_is_vehicle_not_found(self):
        with self.assertRaises(VehicleNotFound):
            self._check("invalid vin")


@override_settings(VINAUDIT_API_KEY="TESTKEY")
class VinAuditHttpErrorTests(SimpleTestCase):
    def test_http_429_is_blocked(self):
        prov = _provider()
        err = requests.HTTPError()
        err.response = type("R", (), {"status_code": 429})()
        with patch.object(prov, "_http_get", side_effect=err):
            with self.assertRaises(BlockedError):
                prov.scrape(VIN)

    def test_network_error_is_scraper_error(self):
        prov = _provider()
        with patch.object(prov, "_http_get", side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(ScraperError):
                prov.scrape(VIN)


class VinAuditNoKeyTests(SimpleTestCase):
    @override_settings(VINAUDIT_API_KEY="")
    def test_missing_key_raises_scraper_error(self):
        prov = _provider()
        # Should not even attempt an HTTP call.
        with patch.object(prov, "_http_get") as http:
            with self.assertRaises(ScraperError):
                prov.scrape(VIN)
        http.assert_not_called()
