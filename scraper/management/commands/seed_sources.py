"""Seed default scraping sources.

Usage:
    python manage.py seed_sources

Idempotent: if a source already exists (by slug), it is updated. Adjust the URL
templates and selectors to each site's real structure.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from scraper.models import ScraperSource

# VinAudit (a paid API) is queried ONLY on-demand, per VIN, in the request path
# (see services.resolve_vin) — NEVER by the background worker/crawler. So its
# `model_path_template` is left EMPTY, which excludes it from scrape_model_data.
# Edmunds is the primary *scraper*; CarGurus is its fallback (worker, by priority).
SOURCES = [
    {
        "slug": "vinaudit",
        "name": "VinAudit Market Value",
        "base_url": "https://marketvalue.vinaudit.com",
        "vin_path_template": "/v2/marketvalue?vin={vin}",
        # EMPTY on purpose: keeps VinAudit OUT of the background model scraping
        # (scrape_model_data excludes sources with no model_path_template). VinAudit
        # is called only on-demand, by VIN, from resolve_vin.
        "model_path_template": "",
        "provider_key": "vinaudit",
        "priority": 5,
        # Only active when a key is set; the on-demand path checks this flag.
        "is_active": bool(getattr(settings, "VINAUDIT_API_KEY", "")),
        "selectors": {},
    },
    {
        "slug": "edmunds",
        "name": "Edmunds",
        "base_url": "https://www.edmunds.com",
        "vin_path_template": "/inventory/vin/{vin}/",
        # The background scraping uses the per-MODEL URL (make/model/year).
        "model_path_template": "/{make}/{model}/{year}/",
        "provider_key": "edmunds",
        "priority": 10,
        "is_active": True,
        "selectors": {
            # Listing price nodes; the median is taken as the model/year market
            # price. Verified against Edmunds' real HTML.
            "model_price_nodes": ".heading-3",
        },
    },
    {
        "slug": "cargurus",
        "name": "CarGurus",
        "base_url": "https://www.cargurus.com",
        "vin_path_template": "/inventory/vin/{vin}/",
        # Placeholder (the cargurus provider builds its own URL from the model's
        # entity id); a non-empty value is required to be eligible for scraping.
        "model_path_template": "/Cars/l-Used-{make}-{model}/",
        "provider_key": "cargurus",
        "priority": 20,  # after Edmunds (10) — used as fallback when it's blocked
        "is_active": True,
        # Listing tile prices (SRP). The provider filters the URL by year.
        "selectors": {"model_price_nodes": "[data-testid=srp-tile-price]"},
    },
]


class Command(BaseCommand):
    help = "Seed the default scraping sources (Edmunds + fallback)."

    def handle(self, *args, **options):
        for data in SOURCES:
            data = dict(data)  # copy: don't mutate module-level SOURCES (idempotent re-runs)
            slug = data.pop("slug")
            source, created = ScraperSource.objects.update_or_create(
                slug=slug, defaults=data
            )
            verb = "Created" if created else "Updated"
            state = "active" if source.is_active else "inactive"
            self.stdout.write(
                self.style.SUCCESS(
                    f"{verb}: {source.name} (priority {source.priority}, {state})"
                )
            )
        if not getattr(settings, "VINAUDIT_API_KEY", ""):
            self.stdout.write(
                self.style.WARNING(
                    "VinAudit source is INACTIVE: set VINAUDIT_API_KEY and re-run "
                    "seed_sources (or activate it in the admin) to use it."
                )
            )
        self.stdout.write(self.style.SUCCESS("Sources seeded."))
