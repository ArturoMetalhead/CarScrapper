"""Regresión de la selección de fuente en scrape_model_data.

Fija el arreglo: una fuente que devuelve un precio SUGERIDO pero sin min/max
(p.ej. Edmunds "suggests you pay $X" para carros nuevos) NO se toma como resultado
final si otra fuente (CarGurus) puede dar un rango real. Así el usuario no ve "solo
el sugerido" en la 1ª búsqueda.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from scraper import services
from scraper.models import ScraperSource
from scraper.providers.base import ScrapedVehicle, VehicleNotFound

# provider_key -> ScrapedVehicle | Exception | None (None => VehicleNotFound)
RESULTS: dict = {}
CALLS: list = []


def _sv(est, low=None, high=None, kind="test"):
    return ScrapedVehicle(
        vin="", make="Honda", model="Pilot", year=2023, trim="",
        estimated_price=Decimal(str(est)),
        price_low=Decimal(str(low)) if low is not None else None,
        price_high=Decimal(str(high)) if high is not None else None,
        price_kind=kind, currency="USD", source_url="http://x", raw_data={},
    )


def _fake_get_provider_class(key):
    class _P:
        def __init__(self, source):
            self.source = source

        def scrape_model(self, make, model, year, trim, series=""):
            CALLS.append(key)
            r = RESULTS.get(key)
            if isinstance(r, Exception):
                raise r
            if r is None:
                raise VehicleNotFound("sin resultados")
            return r

    return _P


class SourceSelectionTest(TestCase):
    def setUp(self):
        RESULTS.clear()
        CALLS.clear()
        ScraperSource.objects.create(
            name="Edmunds", slug="edmunds", provider_key="edmunds",
            model_path_template="/{make}/{model}/{year}/", priority=10, is_active=True,
        )
        ScraperSource.objects.create(
            name="CarGurus", slug="cargurus", provider_key="cargurus",
            model_path_template="/search", priority=20, is_active=True,
        )

    def _run(self):
        with patch.object(services, "get_provider_class", _fake_get_provider_class):
            return services.scrape_model_data("Honda", "Pilot", 2023)

    def test_prefiere_la_fuente_con_rango(self):
        # Edmunds da sugerido SIN rango; CarGurus da rango completo -> gana CarGurus.
        RESULTS["edmunds"] = _sv(30000)
        RESULTS["cargurus"] = _sv(29000, 26000, 34000)
        vm = self._run()
        self.assertEqual(vm.source.name, "CarGurus")
        self.assertEqual(int(vm.price_low), 26000)
        self.assertEqual(int(vm.price_high), 34000)
        self.assertEqual(CALLS, ["edmunds", "cargurus"])  # probó ambas

    def test_edmunds_con_rango_gana_y_no_llama_cargurus(self):
        RESULTS["edmunds"] = _sv(30000, 28000, 35000)
        RESULTS["cargurus"] = RuntimeError("CarGurus NO debería llamarse")
        vm = self._run()
        self.assertEqual(vm.source.name, "Edmunds")
        self.assertNotIn("cargurus", CALLS)  # se detuvo en la primera con rango

    def test_fallback_a_sugerido_si_ninguna_da_rango(self):
        # Edmunds sugerido-solo; CarGurus no encuentra -> usa el sugerido de Edmunds.
        RESULTS["edmunds"] = _sv(30000)
        RESULTS["cargurus"] = None  # VehicleNotFound
        vm = self._run()
        self.assertEqual(vm.source.name, "Edmunds")
        self.assertIsNone(vm.price_low)
        self.assertIsNone(vm.price_high)
