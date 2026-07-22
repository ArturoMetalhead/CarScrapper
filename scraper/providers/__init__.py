"""Scraping providers package.

A *provider* knows how to fetch and parse a vehicle's info from a specific
source. Most sources use `GenericProvider`, configured via CSS selectors from
the database (`ScraperSource` model).

For a site needing special logic (e.g. parsing embedded JSON or calling an
internal API), subclass `BaseProvider` and register it with
`@register("my_key")`. Then set that `provider_key` on the source.
"""
from .base import BaseProvider, ScrapedVehicle
from .generic import GenericProvider, NodriverGenericProvider, PlaywrightGenericProvider
from .registry import get_provider_class, register

# Importing custom providers here registers them on package load.
from . import edmunds  # noqa: F401  (side effect: registers the provider)
from . import cargurus  # noqa: F401  (side effect: registers the fallback provider)

__all__ = [
    "BaseProvider",
    "ScrapedVehicle",
    "GenericProvider",
    "PlaywrightGenericProvider",
    "NodriverGenericProvider",
    "get_provider_class",
    "register",
]
