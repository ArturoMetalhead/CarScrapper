"""Seed default scraping sources.

Usage:
    python manage.py seed_sources

Idempotent: if a source already exists (by slug), it is updated. Adjust the URL
templates and selectors to each site's real structure.
"""
from django.core.management.base import BaseCommand

from scraper.models import ScraperSource

# Edmunds is the primary source (lowest priority = tried first). The others are
# fallbacks: if Edmunds fails or disappears, the system uses them automatically.
SOURCES = [
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
            slug = data.pop("slug")
            source, created = ScraperSource.objects.update_or_create(
                slug=slug, defaults=data
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb}: {source.name} (priority {source.priority})")
            )
        self.stdout.write(self.style.SUCCESS("Sources seeded."))
