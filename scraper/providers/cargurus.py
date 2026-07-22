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
from django.utils import timezone

from .base import BlockedError, ScrapedVehicle, ScraperError, VehicleNotFound
from .edmunds import _PRICE_RE, EdmundsProvider
from .generic import GenericProvider
from .nodriver_fetch import NodriverFetchMixin
from .registry import register

_BASE = "https://www.cargurus.com"
_REF_URL = f"{_BASE}/Cars/getCarPickerReferenceDataAJAX.action"

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

        model_id, cg_name = self._resolve_model(make_id, model, series)
        if not model_id:
            raise VehicleNotFound(f"CarGurus: no model id for {make} {model}.")

        slug = lambda s: "-".join(str(s).strip().split())
        url = f"{_BASE}/Cars/l-Used-{slug(make)}-{slug(cg_name)}-{model_id}"
        if year:
            # startYear/endYear is the param that actually filters by year on
            # CarGurus (minYear/maxYear/year do not), making it year-specific.
            url += f"?startYear={year}&endYear={year}"
        response = self._render(url)
        if self._is_cargurus_block(response.text):
            raise BlockedError(f"CarGurus blocked the request for {make} {model}.")
        if not response.ok:
            raise ScraperError(f"CarGurus responded status {response.status_code}.")

        result = self._parse(response, make, model, year, trim)
        if not result.source_url:
            result.source_url = url
        return result

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

    # --- Price parsing ---------------------------------------------------
    def _parse(
        self, response, make: str, model: str, year=None, trim: str = ""
    ) -> ScrapedVehicle:
        soup = BeautifulSoup(response.text, "lxml")

        # When CarGurus has no listing for the model/year it shows a "0 vehicles
        # found" page whose "Similar cars for you" block reuses the listing tiles
        # with UNRELATED cars. Detect that and bail so we never return those
        # prices (on a page WITH results, the tiles are the real cars).
        text_low = soup.get_text(" ", strip=True).lower()
        if (
            "no cars match your search" in text_low
            or "no exact matches" in text_low
            or re.search(r"(?<!\d)0 vehicles found", text_low)
        ):
            raise VehicleNotFound(f"CarGurus: no listings for {make} {model} {year}.")

        selector = (self.source.selectors or {}).get(
            "model_price_nodes", "[data-testid=srp-tile-price]"
        )
        nodes = soup.select(selector)
        prices: list[Decimal] = []
        for node in nodes:
            m = _PRICE_RE.search(node.get_text(" ", strip=True))
            if not m:
                continue
            try:
                value = Decimal(m.group(1).replace(",", ""))
            except Exception:  # noqa: BLE001
                continue
            if 3000 <= value <= 200000:  # plausible used-car price
                prices.append(value)

        if not prices:
            raise VehicleNotFound(f"CarGurus: no listings for {make} {model} {year}.")

        estimated, low, high = EdmundsProvider._robust_listing_stats(prices)
        return ScrapedVehicle(
            vin="",
            make=make,
            model=model,
            year=year,
            trim=trim or "",
            estimated_price=estimated,
            price_low=low,
            price_high=high,
            price_kind="cargurus_listings_median",
            currency="USD",
            source_url=response.url,
            raw_data={
                "price_kind": "cargurus_listings_median",
                "listing_samples": len(prices),
                "listing_median": float(estimated),
                "range": [float(low), float(high)],
            },
        )
