"""Regresión del parser/agregación de CarGurus (sin red ni BD).

Fija el comportamiento que costó depurar, para que un cambio futuro no lo rompa en
silencio:
  - los TILES del DOM son la fuente real; el JSON embebido "listingId" puede traer
    un widget de "carros recomendados" de OTROS modelos y NO debe usarse;
  - guardia de modelo/año (anti-basura), filtro de trim, orden por relevancia
    (sin sortType=PRICE), distancia nationwide, y BlockedError solo con marcador real.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from scraper.providers.base import BlockedError
from scraper.providers.cargurus import CarGurusProvider


def mk_json(listings):
    """Embedded 'listingId' JSON (widget de recomendaciones / fallback)."""
    objs = []
    for x in listings:
        t = x.get("title")
        tf = "" if t is None else '"listingTitle":"%s",' % t
        objs.append(
            '{"listingId":%d,%s"mileage":%d,"price":%d,"pricePlusFees":%d}'
            % (x["id"], tf, x["mi"], x["price"], x["price"] + 700)
        )
    return "noise " + " junk ".join(objs) + " tail"


def mk_dom(tiles):
    """DOM result tiles (fuente primaria); títulos con el boilerplate de CarGurus."""
    parts = []
    for t in tiles:
        title = "%s Learn more about this %s" % (
            " ".join(t["title"].split()[:3]),
            t["title"],
        )
        parts.append(
            '<div data-testid="srp-listing-tile">'
            '<span data-testid="srp-tile-price">$%s</span>'
            '<span data-testid="srp-tile-listing-title">%s</span></div>'
            % (format(t["price"], ","), title)
        )
    return "<html><body>" + "".join(parts) + "</body></html>"


class _Resp:
    def __init__(self, text):
        self.text = text


def _run(html, model="Tahoe", trim="LT", year=2023):
    prov = CarGurusProvider(source=None)
    calls = []
    prov._render = lambda url: (calls.append(url), _Resp(html))[1]
    result = prov._search_prices("m1", "d639", year, trim, model)
    return result, calls


class SearchUrlTest(SimpleTestCase):
    def test_relevancia_sin_price_sort_y_nationwide(self):
        url = CarGurusProvider(source=None)._search_url("m1", "d639", 2023)
        self.assertNotIn("sortType=PRICE", url)  # orden por defecto = relevancia
        self.assertIn("distance=50000", url)  # nationwide
        self.assertIn("makeModelTrimPaths=m1%2Fd639", url)
        self.assertIn("startYear=2023", url)


class PriceStatsTest(SimpleTestCase):
    def test_min_mediana_max_directo(self):
        est, low, high = CarGurusProvider._price_stats(
            [Decimal("34000"), Decimal("42000"), Decimal("52000")]
        )
        self.assertEqual((low, est, high), (Decimal("34000"), Decimal("42000"), Decimal("52000")))


class SearchPricesTest(SimpleTestCase):
    def test_dom_manda_sobre_json_recomendaciones(self):
        """REGRESIÓN del bug real: DOM=Tahoe, JSON=Nissan -> usa DOM, ignora JSON."""
        dom = mk_dom([
            {"price": 34000, "title": "2023 Chevrolet Tahoe LT 4WD 90,000 mi"},
            {"price": 42000, "title": "2023 Chevrolet Tahoe LT RWD 50,000 mi"},
            {"price": 50507, "title": "2023 Chevrolet Tahoe LT 4WD 34,000 mi"},
            {"price": 30000, "title": "2023 Chevrolet Tahoe LS 4WD 100,000 mi"},
            {"price": 78000, "title": "2023 Chevrolet Tahoe High Country 4WD 20,000 mi"},
            {"price": 28000, "title": "2021 Chevrolet Tahoe LT 4WD 70,000 mi"},  # otro año
        ])
        nissan = mk_json([{"id": 1, "title": "2016 Nissan Sentra S", "mi": 1, "price": 9000}])
        (prices, _url, tm, rows), calls = _run(nissan + dom)
        got = sorted(int(p) for p in prices)
        self.assertEqual(got, [34000, 42000, 50507], "usa DOM LT; ignora Nissan, 2021, LS, HC")
        self.assertEqual(max(int(p) for p in prices), 50507, "MAX = LT caro del DOM")
        self.assertTrue(tm)
        self.assertEqual(len(calls), 1, "una sola consulta")
        # títulos guardados limpios (sin boilerplate)
        self.assertTrue(all("Learn more about this" not in (r["title"] or "") for r in rows))

    def test_pagina_sin_modelo_es_not_found_no_block(self):
        """Página degradada/sin inventario (sin marcador) -> vacío, NO BlockedError
        (para no envenenar el estado de bloqueo del worker)."""
        (prices, _url, _tm, _rows), _ = _run(
            mk_json([{"id": 1, "title": "2020 Hyundai Kona SEL", "mi": 1, "price": 17000}])
        )
        self.assertEqual(prices, [])

    def test_marcador_perimeterx_es_block(self):
        with self.assertRaises(BlockedError):
            _run("noise px-captcha challenge")

    def test_pocos_del_trim_cae_a_todas_las_versiones(self):
        dom = mk_dom([
            {"price": 34000, "title": "2023 Chevrolet Tahoe LT 4WD 90,000 mi"},
            {"price": 30000, "title": "2023 Chevrolet Tahoe LS 4WD 100,000 mi"},
            {"price": 78000, "title": "2023 Chevrolet Tahoe High Country 4WD 20,000 mi"},
        ])
        (prices, _url, tm, _rows), _ = _run(dom)
        self.assertFalse(tm, "1 LT -> all-trims")
        self.assertIn(78000, [int(p) for p in prices], "all-trims incluye el techo del modelo")

    def test_fallback_json_cuando_no_hay_tiles_dom(self):
        json_tahoe = mk_json([
            {"id": 1, "title": "2023 Chevrolet Tahoe LT 4WD", "mi": 40000, "price": 45000},
            {"id": 2, "title": "2023 Chevrolet Tahoe LT RWD", "mi": 30000, "price": 47000},
            {"id": 3, "title": "2023 Chevrolet Tahoe LT 4WD", "mi": 20000, "price": 49000},
        ])
        (prices, _url, tm, _rows), _ = _run(json_tahoe)
        self.assertEqual(sorted(int(p) for p in prices), [45000, 47000, 49000])
        self.assertTrue(tm)


class ExtractorsTest(SimpleTestCase):
    def test_json_extrae_precio_km_titulo_year(self):
        html = mk_json([{"id": 7, "title": "2023 Chevrolet Tahoe LT 4WD", "mi": 36281, "price": 48240}])
        rows = CarGurusProvider._extract_json_listings(html)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["price"], r["mileage"], r["year"]), (Decimal("48240"), 36281, 2023))
        self.assertEqual(r["title"], "2023 Chevrolet Tahoe LT 4WD")

    def test_dom_limpia_boilerplate_y_extrae_year(self):
        html = mk_dom([{"price": 44000, "title": "2023 Chevrolet Tahoe LT 4WD 30,000 mi"}])
        rows = CarGurusProvider._extract_dom_listings(html)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertNotIn("Learn more about this", r["title"])
        self.assertEqual((r["price"], r["mileage"], r["year"]), (Decimal("44000"), 30000, 2023))
