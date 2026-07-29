"""Prueba la LÓGICA de comparación 'parecido a antes' (con tolerancia, por fuente).

Garantiza que:
  - un cambio PEQUEÑO de rango (dentro de la tolerancia) se considera OK — NO se
    exige que el valor sea exactamente el de la BD;
  - un cambio GRANDE (fuera de la tolerancia) se marca como divergencia;
  - Edmunds y CarGurus se comparan por SEPARADO (nunca uno contra el otro); si un
    modelo cambió de fuente, se reporta aparte y NO como regresión.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from scraper.management.commands.snapshot_prices import compare


def row(source, low, est, high, make="Honda", model="Civic", year=2023, trim=""):
    return {
        "make": make, "model": model, "year": year, "trim": trim,
        "source": source, "low": low, "est": est, "high": high,
    }


class CompareToleranceTest(SimpleTestCase):
    TOL = 0.30

    def test_cambio_pequeno_es_parecido_ok(self):
        base = [row("CarGurus", 10000, 12000, 15000)]
        cur = [row("CarGurus", 10400, 12300, 15600)]  # ~+3-4%, dentro de tolerancia
        res = compare(base, cur, self.TOL)
        self.assertEqual(res["ok"], 1)
        self.assertEqual(res["diverged"], [])

    def test_justo_en_el_borde_pasa(self):
        base = [row("CarGurus", 10000, 12000, 15000)]
        cur = [row("CarGurus", 10000, 12000, 15000 * 1.30)]  # +30% exacto (no supera)
        res = compare(base, cur, self.TOL)
        self.assertEqual(res["diverged"], [])

    def test_cambio_grande_se_marca(self):
        base = [row("CarGurus", 10000, 12000, 15000)]
        cur = [row("CarGurus", 5000, 6000, 15000)]  # -50% en low/est
        res = compare(base, cur, self.TOL)
        self.assertEqual(len(res["diverged"]), 1)
        _b, _c, deltas = res["diverged"][0]
        self.assertAlmostEqual(deltas["est"], -0.5, places=6)

    def test_por_fuente_no_cruza_edmunds_cargurus(self):
        # MISMO modelo en distinta fuente: NO es divergencia (se comparan aparte),
        # se reporta como cambio de fuente.
        base = [row("Edmunds", 10000, 12000, 15000)]
        cur = [row("CarGurus", 20000, 25000, 30000)]
        res = compare(base, cur, self.TOL)
        self.assertEqual(res["diverged"], [])
        self.assertEqual(len(res["source_changed"]), 1)

    def test_misma_fuente_si_compara(self):
        base = [
            row("Edmunds", 10000, 12000, 15000),
            row("CarGurus", 20000, 25000, 30000, model="Pilot"),
        ]
        cur = [
            row("Edmunds", 18000, 20000, 24000),  # Edmunds subió mucho -> divergencia
            row("CarGurus", 20200, 25100, 30300, model="Pilot"),  # CarGurus casi igual -> ok
        ]
        res = compare(base, cur, self.TOL)
        self.assertEqual(res["ok"], 1)
        self.assertEqual(len(res["diverged"]), 1)
        self.assertEqual(res["diverged"][0][1]["source"], "Edmunds")

    def test_faltante_se_reporta(self):
        base = [row("CarGurus", 10000, 12000, 15000, model="Fit")]
        cur = []
        res = compare(base, cur, self.TOL)
        self.assertEqual(len(res["missing"]), 1)
