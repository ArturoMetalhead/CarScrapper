"""Invariantes sobre el baseline de precios (scraper/tests/fixtures/price_baseline.json).

Estos tests NO tocan la red ni la BD: validan que la "foto" de rangos que tenemos
sea sana. Sirven para que, si alguien regenera el baseline tras cambiar el scraper
(`manage.py snapshot_prices`), un rango absurdo (invertido, en cero, disparado) haga
fallar el test en vez de colarse.

Para comparar rangos NUEVOS contra este baseline POR FUENTE (Edmunds vs CarGurus),
usa el comando: `python manage.py snapshot_prices --check`.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.test import SimpleTestCase

BASELINE = Path(__file__).resolve().parent / "fixtures" / "price_baseline.json"

# Cotas absolutas de sanidad (no de mercado): un precio de carro plausible.
MIN_PLAUSIBLE = 300
MAX_PLAUSIBLE = 10_000_000
# high/low máximo tolerado: solo para cazar absurdos (rango disparado), no mercado.
MAX_RATIO = 40.0


def _load():
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    return data, data["models"]


class PriceBaselineInvariantsTest(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.data, cls.models = _load()

    def test_fixture_no_vacio_y_coherente(self):
        self.assertTrue(self.models, "el baseline está vacío")
        self.assertEqual(
            self.data["_meta"]["count"],
            len(self.models),
            "_meta.count no coincide con el nº de modelos",
        )

    def test_cada_entrada_tiene_campos(self):
        req = {"make", "model", "year", "trim", "source", "low", "est", "high"}
        for m in self.models:
            with self.subTest(model=self._label(m)):
                self.assertTrue(req <= set(m), f"faltan campos: {req - set(m)}")

    def test_orden_low_est_high(self):
        """El invariante más importante: mínimo <= sugerido <= máximo, y > 0."""
        for m in self.models:
            with self.subTest(model=self._label(m)):
                low, est, high = m["low"], m["est"], m["high"]
                self.assertGreater(low, 0, "low <= 0")
                self.assertLessEqual(low, est, "low > est (rango invertido)")
                self.assertLessEqual(est, high, "est > high (rango invertido)")

    def test_dentro_de_cotas_plausibles(self):
        for m in self.models:
            with self.subTest(model=self._label(m)):
                self.assertGreaterEqual(m["low"], MIN_PLAUSIBLE, "low por debajo de lo plausible")
                self.assertLessEqual(m["high"], MAX_PLAUSIBLE, "high por encima de lo plausible")

    def test_ratio_no_absurdo(self):
        """high/low no debe estar disparado (cazar un rango roto, no el mercado)."""
        for m in self.models:
            with self.subTest(model=self._label(m)):
                ratio = m["high"] / m["low"] if m["low"] else float("inf")
                self.assertLessEqual(
                    ratio, MAX_RATIO, f"ratio high/low = {ratio:.1f} (>{MAX_RATIO})"
                )

    def test_ambas_fuentes_presentes(self):
        sources = {m["source"] for m in self.models}
        self.assertIn("Edmunds", sources)
        self.assertIn("CarGurus", sources)

    def test_resumen_por_fuente(self):
        """No asevera; imprime el perfil por fuente (Edmunds vs CarGurus difieren)."""
        from statistics import median

        by_source: dict[str, list] = {}
        for m in self.models:
            by_source.setdefault(m["source"], []).append(m)
        lines = ["", "Perfil por fuente (informativo):"]
        for src, rows in sorted(by_source.items()):
            ratios = [r["high"] / r["low"] for r in rows if r["low"]]
            ests = [r["est"] for r in rows]
            lines.append(
                f"  {src}: n={len(rows)} | est mediana=${median(ests):,.0f} | "
                f"ratio high/low mediano={median(ratios):.1f} máx={max(ratios):.1f}"
            )
        print("\n".join(lines))

    @staticmethod
    def _label(m: dict) -> str:
        return f"{m['make']} {m['model']} {m['year']} {m['trim'] or '-'} [{m['source']}]"
