"""CarGurus provider (https://www.cargurus.com/) — fallback source.

Used when the primary source (Edmunds) is blocked. CarGurus model pages only
render with an internal model entity id (e.g. BMW 3 Series -> "d1512"), not from
make/model text. We resolve that id from CarGurus' own reference endpoint
(`getCarPickerReferenceDataAJAX.action`, which maps makeId -> models -> modelId)
plus a small, stable make-name -> makeId map, then scrape the listing page and
aggregate the prices (median + trimmed range), like the Edmunds provider.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from statistics import median

from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from .base import BlockedError, ScrapedVehicle, ScraperError, VehicleNotFound
from .edmunds import _PRICE_MAX, _PRICE_MIN, _PRICE_RE, trim_regex
from .generic import GenericProvider
from .nodriver_fetch import NodriverFetchMixin
from .registry import register

_BASE = "https://www.cargurus.com"
_REF_URL = f"{_BASE}/Cars/getCarPickerReferenceDataAJAX.action"
# Price-sorted search endpoint; server-rendered and filterable by make/model/year.
_SEARCH_URL = f"{_BASE}/search"

# Stable make-name -> CarGurus makeId (from CarGurus' own make links).
_MAKE_IDS = {
    "chevrolet": "m1", "ford": "m2", "bmw": "m3", "acura": "m4", "honda": "m6",
    "toyota": "m7", "nissan": "m12", "audi": "m19", "buick": "m21", "cadillac": "m22",
    "chrysler": "m23", "dodge": "m24", "gmc": "m26", "hyundai": "m28", "jeep": "m32",
    "kia": "m33", "land rover": "m35", "lexus": "m37", "lincoln": "m38", "mazda": "m42",
    "mercedes-benz": "m43", "mercedes": "m43", "mitsubishi": "m46", "porsche": "m48",
    "subaru": "m53", "volkswagen": "m55", "volvo": "m56", "infiniti": "m84",
    "tesla": "m112", "ram": "m191", "genesis": "m203",
}

# CarGurus reference data cache (makeId -> [{modelName, modelId}, ...]).
_REF_CACHE: dict = {"models": None, "ts": None}
_REF_TTL_SECONDS = 24 * 3600

# Anti-bot markers specific to CarGurus (PerimeterX).
_CG_BLOCK = ("px-captcha", "perimeterx", "access to this page has been denied")


def _norm(text: str) -> str:
    """Normalize a model name for matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _title_year(title: str):
    """Extract the leading 4-digit model year from a listing title
    ("2023 Chevrolet Tahoe LT 4WD" -> 2023). None if absent/implausible."""
    m = re.match(r"\s*(\d{4})\b", title or "")
    if not m:
        return None
    year = int(m.group(1))
    return year if 1900 <= year <= 2100 else None


@register("cargurus")
class CarGurusProvider(NodriverFetchMixin, GenericProvider):
    """CarGurus scraper (fallback). Resolves the model entity id, then scrapes."""

    def scrape_model(
        self, make: str, model: str, year=None, trim: str = "", series: str = ""
    ) -> ScrapedVehicle:
        make_id = _MAKE_IDS.get((make or "").strip().lower())
        if not make_id:
            raise VehicleNotFound(f"CarGurus: make '{make}' not in the id map.")

        model_id, cg_name = self._resolve_model(make_id, model, series)
        if not model_id:
            raise VehicleNotFound(f"CarGurus: no model id for {make} {model}.")

        prices, url, trim_matched, rows = self._search_prices(
            make_id, model_id, year, trim, cg_name or model
        )
        if not prices:
            raise VehicleNotFound(
                f"CarGurus: no {trim} listings for {make} {model} {year}.".replace("  ", " ")
            )

        # If a trim was requested but too few matched, we fell back to ALL trims;
        # flag it (distinct kind + raw_data) so the row is NOT taken as trim-accurate.
        degraded = bool(trim) and not trim_matched
        kind = "cargurus_alltrims_median" if degraded else "cargurus_listings_median"
        estimated, low, high = self._price_stats(prices)
        # A few real example listings (price + mileage + title) so the estimate is
        # auditable and future price bugs aren't invisible. Stored under a DISTINCT
        # key: `listing_samples` stays an int count (admin/worker read it as such,
        # like Edmunds), `listing_examples` holds the list.
        examples = [
            {
                "price": float(r["price"]),
                "mileage": r["mileage"],
                "title": r.get("title", ""),
            }
            for r in rows[:12]
        ]
        return ScrapedVehicle(
            vin="", make=make, model=model, year=year, trim=trim or "",
            estimated_price=estimated, price_low=low, price_high=high,
            price_kind=kind, currency="USD", source_url=url,
            raw_data={
                "price_kind": kind,
                "listing_samples": len(prices),
                "listing_examples": examples,
                "listing_median": float(estimated),
                "range": [float(low), float(high)],
                "trim": trim or None,
                "trim_matched": None if not trim else trim_matched,
            },
        )

    @staticmethod
    def _price_stats(prices):
        """(median, min, max) computed DIRECTLY — not Edmunds' band-trimmed stats.

        The ASC+DESC combine is deliberately bimodal (a cheap cluster + a dear
        cluster), so a band around the median would drop a whole cluster and could
        shove the low/centre into the dear side, throwing away the real floor the
        ASC fetch exists to capture. We WANT the true floor (min) and ceiling (max);
        the model + price-range + trim filters already removed cross-sell noise, so
        no trimming is needed."""
        return Decimal(round(median(prices))), min(prices), max(prices)

    def _search_url(self, make_id: str, model_id: str, year, sort_dir=None) -> str:
        """/search URL for a make/model (all trims). Default order is CarGurus' "best
        match" (relevance), which surfaces a representative price spread of the model
        — a mid trim's dearest examples sit in the MIDDLE of the price ladder, so a
        price sort (ASC or DESC) would miss them and understate the trim's max. Pass
        sort_dir only if a price sort is explicitly wanted. The trim is filtered from
        the listing titles in code, because CarGurus needs the EXACT trim name (e.g.
        "Sport AWD") which NHTSA does not provide."""
        zip_code = getattr(settings, "SCRAPER_CARGURUS_ZIP", "07047")
        # distance=50000 is CarGurus' "Nationwide" radius (its widest option — the US
        # is ~2,800 mi across), so we see the whole market's spread, not a local slice.
        distance = getattr(settings, "SCRAPER_CARGURUS_DISTANCE", 50000)
        params = [
            f"zip={zip_code}", f"distance={distance}",
            f"makeModelTrimPaths={make_id}%2F{model_id}",
        ]
        if sort_dir:
            params += ["sortType=PRICE", f"sortDirection={sort_dir}"]
        if year:
            # startYear/endYear is the param that actually filters by year.
            params += [f"startYear={year}", f"endYear={year}"]
        return f"{_SEARCH_URL}?{'&'.join(params)}"

    def _search_prices(self, make_id, model_id, year, trim, model_name):
        """Fetch the default-relevance /search results; return
        (prices, url, trim_matched, rows).

        NOT price-sorted: sorting by price biases the sample to one end and, for a
        mid trim like LT (whose dearest examples sit in the MIDDLE of the price
        ladder), even a combined ASC+DESC misses them and understates the trim's max.
        CarGurus' "best match" order surfaces a representative spread of the version
        in a single fetch. Each listing's price / mileage / exact-trim title is read
        from the embedded JSON (DOM fallback if that shape changes). Rows are filtered
        to the requested MODEL and YEAR (a degraded page or an embedded "recommended"
        block leaks other cars) and then to the TRIM, so the min / median / MAX
        reflect the exact version searched. `rows` is the {price, mileage, title}
        list actually used.
        """
        url = self._search_url(make_id, model_id, year)
        resp = self._render(url)
        if self._is_cargurus_block(resp.text):
            raise BlockedError(f"CarGurus blocked the {model_name} request (PerimeterX).")
        # The DOM result tiles are the reliable list of the model's actual results.
        # The embedded "listingId" JSON is NOT: CarGurus often fills it with a
        # "recommended / similar cheaper cars" widget of OTHER models (Nissan,
        # Hyundai…) that would poison the aggregate. Parse the tiles first; only if
        # their markup changed (none found) fall back to the JSON, and let the model
        # guard below drop any non-model rows that leak in.
        listings = self._extract_dom_listings(resp.text) or self._extract_json_listings(
            resp.text
        )

        priced = [r for r in listings if _PRICE_MIN <= r["price"] <= _PRICE_MAX]
        # MODEL GUARD (anti-garbage): a soft-blocked/degraded page — or an
        # empty-inventory "similar cars" page — parses as listings of OTHER models.
        # Keep only rows whose title names the requested model, so we never aggregate
        # other cars' prices under this model.
        model_token = (model_name or "").strip().lower()
        model_rows = (
            [r for r in priced if model_token in r["title"].lower()]
            if model_token
            else priced
        )
        # YEAR GUARD: the page is year-filtered via the URL, but an embedded
        # "recommended / recently-viewed" block can leak other model-years into the
        # HTML. Drop rows whose parsed title-year differs from the one requested.
        if year and model_rows:
            model_rows = [r for r in model_rows if r.get("year") in (None, int(year))]
        if not model_rows:
            # The page wasn't PerimeterX-blocked (checked above); it just held no
            # listing of the requested model (empty inventory / "similar cars"), so
            # this is a genuine not-found. Returning [] (vs BlockedError) avoids
            # poisoning the worker's global block state for a page never blocked.
            return [], url, (None if not trim else False), []

        trim_re = trim_regex(trim) if trim else None
        trim_rows = (
            [r for r in model_rows if trim_re.search(r["title"])] if trim_re else []
        )
        # Prefer the trim-filtered prices; if the trim matched too few, fall back to
        # ALL trims (flagged) so we still return a model-level range. A representative
        # page is thinner per-trim, so accept a smaller floor than Edmunds.
        min_n = getattr(settings, "SCRAPER_CARGURUS_TRIM_MIN_LISTINGS", 3)
        trim_matched = trim_re is not None and len(trim_rows) >= min_n
        rows = trim_rows if trim_matched else model_rows
        prices = [r["price"] for r in rows]
        return prices, url, (None if trim_re is None else trim_matched), rows

    @staticmethod
    def _extract_json_listings(html: str) -> list[dict]:
        """FALLBACK parser for CarGurus' embedded per-listing JSON (used only when the
        DOM tiles can't be read). WARNING: the "listingId" JSON is NOT always the
        model's results — CarGurus can fill it with a "recommended cheaper cars"
        widget of OTHER models (Nissan, Hyundai…), so callers MUST filter by model
        afterwards. Each object carries `listingTitle`, an integer `price` and
        `mileage`; splitting on `"listingId"` scopes each field regex to one listing.
        Returns [{price: Decimal, mileage: int|None, title: str, year: int|None}, ...]."""
        rows: list[dict] = []
        for chunk in html.split('"listingId"')[1:]:
            # Price is normally an unquoted int ("price":48240); tolerate a quoted or
            # comma-grouped form too so a formatting change doesn't silently break.
            price_m = re.search(r'"price"\s*:\s*"?([\d,]+)', chunk)
            if not price_m:
                continue
            try:
                price = Decimal(price_m.group(1).replace(",", ""))
            except Exception:  # noqa: BLE001
                continue
            # Title regex tolerates escaped quotes (\") and any length — a fixed cap
            # would silently drop a listing whose title happened to fail to match.
            title_m = re.search(r'"listingTitle"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
            title = title_m.group(1) if title_m else ""
            mileage_m = re.search(r'"mileage"\s*:\s*"?([\d,]+)', chunk)
            rows.append(
                {
                    "price": price,
                    "mileage": int(mileage_m.group(1).replace(",", "")) if mileage_m else None,
                    "title": title,
                    "year": _title_year(title),
                }
            )
        return rows

    @staticmethod
    def _extract_dom_listings(html: str) -> list[dict]:
        """Fallback: scrape the DOM tiles if the embedded JSON shape changes."""
        soup = BeautifulSoup(html, "lxml")
        rows: list[dict] = []
        for card in soup.select("[data-testid=srp-listing-tile]"):
            price_el = card.select_one("[data-testid=srp-tile-price]")
            if price_el is None:
                continue
            m = _PRICE_RE.search(price_el.get_text(" ", strip=True))
            if not m:
                continue
            try:
                value = Decimal(m.group(1).replace(",", ""))
            except Exception:  # noqa: BLE001
                continue
            title_el = card.select_one("[data-testid=srp-tile-listing-title]")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            # Tiles pad the title with a "... Learn more about this <real title>"
            # prefix; keep the meaningful tail (still starts with the year).
            title = title.split("Learn more about this")[-1].strip()
            mi_m = re.search(r"([\d,]+)\s*mi\b", title)
            rows.append(
                {
                    "price": value,
                    "mileage": int(mi_m.group(1).replace(",", "")) if mi_m else None,
                    "title": title,
                    "year": _title_year(title),
                }
            )
        return rows

    # --- Model-id resolution --------------------------------------------
    def _resolve_model(self, make_id: str, model: str, series: str) -> tuple[str, str]:
        """Return (modelId, cargurusModelName) for a make's model, or ('','')."""
        models = self._reference().get(make_id, [])
        index = {_norm(m.get("modelName", "")): m for m in models if m.get("modelId")}
        for candidate in (series, model):
            hit = index.get(_norm(candidate))
            if hit:
                return hit["modelId"], hit["modelName"]
        # loose fallback: a CarGurus name that starts with the model/series token
        token = _norm(series) or _norm(model)
        for norm_name, m in index.items():
            if token and (norm_name.startswith(token) or token.startswith(norm_name)):
                return m["modelId"], m["modelName"]
        return "", ""

    def _reference(self) -> dict:
        """makeId -> list of {modelName, modelId}, cached (fetched via browser)."""
        now = timezone.now()
        if _REF_CACHE["models"] and _REF_CACHE["ts"] and (
            (now - _REF_CACHE["ts"]).total_seconds() < _REF_TTL_SECONDS
        ):
            return _REF_CACHE["models"]

        resp = self._render(_REF_URL)
        # A PerimeterX challenge here would otherwise become a cryptic JSONDecodeError
        # that bypasses the worker's block recovery; surface it as a BlockedError.
        if self._is_cargurus_block(resp.text):
            raise BlockedError("CarGurus blocked the reference request.")
        match = re.search(r"\{.*\}", resp.text, re.S)
        data = json.loads(match.group(0)) if match else {}
        raw = data.get("allMakerModels", {})
        models: dict[str, list] = {}
        for make_id, block in raw.items():
            flat: list = []
            if isinstance(block, dict):
                for group in block.values():
                    if isinstance(group, list):
                        flat += group
            models[make_id] = flat
        _REF_CACHE["models"] = models
        _REF_CACHE["ts"] = now
        return models

    @staticmethod
    def _is_cargurus_block(html: str) -> bool:
        low = html.lower()
        return any(marker in low for marker in _CG_BLOCK)
