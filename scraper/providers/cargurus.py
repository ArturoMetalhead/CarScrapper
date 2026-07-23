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
from .edmunds import _PRICE_RE, EdmundsProvider, trim_regex
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


@register("cargurus")
class CarGurusProvider(NodriverFetchMixin, GenericProvider):
    """CarGurus scraper (fallback). Resolves the model entity id, then scrapes."""

    def scrape_model(
        self, make: str, model: str, year=None, trim: str = "", series: str = ""
    ) -> ScrapedVehicle:
        make_id = _MAKE_IDS.get((make or "").strip().lower())
        if not make_id:
            raise VehicleNotFound(f"CarGurus: make '{make}' not in the id map.")

        model_id, _cg_name = self._resolve_model(make_id, model, series)
        if not model_id:
            raise VehicleNotFound(f"CarGurus: no model id for {make} {model}.")

        prices, url = self._search_prices(make_id, model_id, year, trim)
        if not prices:
            raise VehicleNotFound(
                f"CarGurus: no {trim} listings for {make} {model} {year}.".replace("  ", " ")
            )

        estimated, low, high = EdmundsProvider._robust_listing_stats(prices)
        return ScrapedVehicle(
            vin="", make=make, model=model, year=year, trim=trim or "",
            estimated_price=estimated, price_low=low, price_high=high,
            price_kind="cargurus_listings_median", currency="USD", source_url=url,
            raw_data={
                "price_kind": "cargurus_listings_median",
                "listing_samples": len(prices),
                "listing_median": float(estimated),
                "range": [float(low), float(high)],
                "trim": trim or None,
            },
        )

    def _search_url(self, make_id: str, model_id: str, year, sort_dir: str) -> str:
        """Price-sorted /search URL for a make/model (all trims). The trim is
        filtered from the card titles in code, because CarGurus needs the EXACT
        trim name (e.g. "Sport AWD") which NHTSA does not provide."""
        zip_code = getattr(settings, "SCRAPER_CARGURUS_ZIP", "07047")
        params = [
            f"zip={zip_code}", "sortType=PRICE", f"sortDirection={sort_dir}",
            "distance=50000", f"makeModelTrimPaths={make_id}%2F{model_id}",
        ]
        if year:
            # startYear/endYear is the param that actually filters by year.
            params += [f"startYear={year}", f"endYear={year}"]
        return f"{_SEARCH_URL}?{'&'.join(params)}"

    def _search_prices(self, make_id: str, model_id: str, year, trim: str):
        """Fetch the price-ascending /search results and return (prices, url).

        Each listing card carries its full title (e.g. "... Honda HR-V Sport AWD
        ..."); when `trim` is given (VIN searches) only cards whose title matches
        that trim are kept, so a Sport search doesn't pick up LX prices. Ascending
        surfaces the real floor and a representative low-to-mid spread in one fetch.
        """
        url = self._search_url(make_id, model_id, year, "ASC")
        resp = self._render(url)
        if self._is_cargurus_block(resp.text):
            raise BlockedError(f"CarGurus blocked the request (make {make_id}).")
        soup = BeautifulSoup(resp.text, "lxml")
        trim_re = trim_regex(trim) if trim else None
        all_prices: list[Decimal] = []
        trim_prices: list[Decimal] = []
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
            if not (3000 <= value <= 200000):  # plausible used-car price
                continue
            all_prices.append(value)
            if trim_re is not None:
                title_el = card.select_one("[data-testid=srp-tile-listing-title]")
                title = title_el.get_text(" ", strip=True) if title_el else ""
                if trim_re.search(title):
                    trim_prices.append(value)
        # Prefer the trim-filtered prices; if the trim matched too few (a naming
        # mismatch or thin old-car inventory), fall back to ALL trims so we still
        # return a model-level range rather than "not found".
        min_n = getattr(settings, "SCRAPER_EDMUNDS_MIN_LISTINGS", 5)
        prices = trim_prices if (trim_re is not None and len(trim_prices) >= min_n) else all_prices
        return prices, url

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
