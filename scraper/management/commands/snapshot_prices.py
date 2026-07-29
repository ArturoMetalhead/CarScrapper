"""Snapshot y regresión de los rangos de precio scrapeados.

Objetivo: si en el futuro se cambia el scraper, poder ver que los rangos que
devuelve siguen siendo PARECIDOS a los de antes — comparando SIEMPRE por fuente
(Edmunds vs CarGurus se comparan por separado, porque sus precios difieren mucho).

Uso:
  python manage.py snapshot_prices
      Regenera el baseline (scraper/tests/fixtures/price_baseline.json) con los
      rangos actuales de la BD. Commitéalo: es la "foto" de referencia.

  python manage.py snapshot_prices --check [--tolerance 0.30]
      Compara la BD ACTUAL contra el baseline y reporta, POR FUENTE, los modelos
      cuyo mínimo/sugerido/máximo se movió más que la tolerancia. Corre esto
      después de tocar el scraper y re-scrapear. Sale con código 1 si hay
      divergencias (para usarlo como puerta en CI).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand

from scraper.models import VehicleModel

BASELINE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "price_baseline.json"
)


def _count(raw) -> int | None:
    """Best-effort nº de anuncios usados (Edmunds y CarGurus lo guardan distinto)."""
    if not isinstance(raw, dict):
        return None
    value = raw.get("listing_samples")
    if value is None:
        value = raw.get("market_listings")
    if isinstance(value, list):
        return len(value)
    return value if isinstance(value, int) else None


def _key(entry: dict) -> tuple:
    """Clave por (make, model, year, trim, FUENTE) — incluir la fuente asegura que
    solo se compare Edmunds-con-Edmunds y CarGurus-con-CarGurus."""
    return (
        (entry["make"] or "").strip().lower(),
        (entry["model"] or "").strip().lower(),
        entry["year"],
        (entry["trim"] or "").strip().lower(),
        entry["source"],
    )


def _trim_key(entry: dict) -> tuple:
    """Igual que _key pero SIN la fuente — para detectar cambios de fuente."""
    return _key(entry)[:4]


def compare(baseline_rows: list[dict], current_rows: list[dict], tolerance: float) -> dict:
    """Comparación PURA y con TOLERANCIA (parecido, NO exacto) de rangos actuales vs
    baseline. Empareja por (make, model, year, trim, FUENTE), así que NUNCA compara
    Edmunds contra CarGurus. Un cambio de low/est/high dentro de ±tolerance cuenta
    como "parecido" (ok); por encima, como divergencia.

    Devuelve: {
        "ok": int,                       # nº de modelos parecidos
        "diverged": [(base, cur, deltas)],  # deltas = {campo: cambio_relativo}
        "source_changed": [(prev, cur)],    # mismo modelo, otra fuente (esperado)
        "missing": [base],                  # en baseline y ya no en la BD
    }
    """
    base = {_key(e): e for e in baseline_rows}
    cur = {_key(e): e for e in current_rows}
    ok = 0
    diverged: list = []
    for key in base.keys() & cur.keys():
        b, c = base[key], cur[key]
        deltas = {
            field: (c[field] - b[field]) / b[field]
            for field in ("low", "est", "high")
            if b.get(field)
        }
        worst = max((abs(d) for d in deltas.values()), default=0.0)
        if worst > tolerance:
            diverged.append((b, c, deltas))
        else:
            ok += 1

    base_by_trim = {_trim_key(e): e for e in baseline_rows}
    source_changed = []
    for key in cur.keys() - base.keys():
        prev = base_by_trim.get(key[:4])
        if prev and prev["source"] != cur[key]["source"]:
            source_changed.append((prev, cur[key]))
    cur_trims = {_key(e)[:4] for e in current_rows}
    missing = [
        b for k, b in base.items() if k not in cur and _key(b)[:4] not in cur_trims
    ]
    return {
        "ok": ok,
        "diverged": diverged,
        "source_changed": source_changed,
        "missing": missing,
    }


class Command(BaseCommand):
    help = "Genera/compara el baseline de rangos de precio (por fuente)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Compara la BD contra el baseline en vez de regenerarlo.",
        )
        parser.add_argument(
            "--tolerance",
            type=float,
            default=0.30,
            help="Cambio relativo permitido antes de marcar divergencia (0.30 = 30%%).",
        )

    # -- extracción --------------------------------------------------------
    def _rows(self) -> list[dict]:
        rows = []
        qs = (
            VehicleModel.objects.filter(estimated_price__isnull=False)
            .select_related("source")
            .order_by("make", "model", "year", "trim")
        )
        for vm in qs:
            if vm.price_low is None or vm.price_high is None:
                continue
            rows.append(
                {
                    "make": vm.make,
                    "model": vm.model,
                    "year": vm.year,
                    "trim": vm.trim or "",
                    "source": vm.source.name if vm.source else "?",
                    "price_kind": vm.price_kind or "",
                    "low": float(vm.price_low),
                    "est": float(vm.estimated_price),
                    "high": float(vm.price_high),
                    "n": _count(vm.raw_data),
                }
            )
        return rows

    def handle(self, *args, **opts):
        if opts["check"]:
            self._check(opts["tolerance"])
        else:
            self._generate()

    # -- generar -----------------------------------------------------------
    def _generate(self):
        rows = self._rows()
        by_source: dict[str, int] = {}
        for r in rows:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        data = {
            "_meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(rows),
                "by_source": by_source,
                "note": (
                    "Baseline de rangos de precio por (make, model, year, trim, fuente). "
                    "Regenerar con `manage.py snapshot_prices`; comparar con `--check`."
                ),
            },
            "models": rows,
        }
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Baseline escrito: {BASELINE} ({len(rows)} modelos; {by_source})"
            )
        )

    # -- comparar ----------------------------------------------------------
    def _check(self, tol: float):
        if not BASELINE.exists():
            self.stderr.write(
                self.style.ERROR(
                    "No hay baseline. Corre `python manage.py snapshot_prices` primero."
                )
            )
            raise SystemExit(2)
        base_rows = json.loads(BASELINE.read_text(encoding="utf-8"))["models"]
        res = compare(base_rows, self._rows(), tol)

        self.stdout.write(
            f"Tolerancia: ±{tol:.0%} | comparados OK (parecidos): {res['ok']}"
        )
        by_source: dict[str, list] = {}
        for b, c, deltas in res["diverged"]:
            by_source.setdefault(c["source"], []).append((b, c, deltas))
        if not res["diverged"]:
            self.stdout.write(
                self.style.SUCCESS("Sin divergencias por encima de la tolerancia.")
            )
        for source in sorted(by_source):
            rows = by_source[source]
            self.stdout.write(self.style.WARNING(f"\n=== {source}: {len(rows)} divergen ==="))
            for b, c, deltas in sorted(rows, key=lambda t: -max(abs(d) for d in t[2].values())):
                label = f"{c['make']} {c['model']} {c['year']} {c['trim'] or '-'}"
                parts = " ".join(
                    f"{f}:{b[f]:.0f}->{c[f]:.0f}({deltas[f]:+.0%})"
                    for f in ("low", "est", "high")
                    if f in deltas
                )
                self.stdout.write(f"  {label}  {parts}")

        if res["source_changed"]:
            self.stdout.write(
                self.style.NOTICE(
                    f"\n=== Cambió de fuente: {len(res['source_changed'])} (diferencia esperada) ==="
                )
            )
            for prev, c in res["source_changed"]:
                label = f"{c['make']} {c['model']} {c['year']} {c['trim'] or '-'}"
                self.stdout.write(
                    f"  {label}  {prev['source']}({prev['est']:.0f}) -> "
                    f"{c['source']}({c['est']:.0f})"
                )

        if res["missing"]:
            self.stdout.write(
                self.style.NOTICE(
                    f"\n=== En baseline pero ya no en BD: {len(res['missing'])} ==="
                )
            )
            for b in res["missing"][:20]:
                self.stdout.write(
                    f"  {b['make']} {b['model']} {b['year']} {b['trim'] or '-'} ({b['source']})"
                )

        if res["diverged"]:
            raise SystemExit(1)
