"""Discover models (via NHTSA) and enqueue crawl jobs.

One-shot version of what the background planner does. Useful to kick off or top
up the crawl manually.

Usage:
    python manage.py crawl                       # default makes/years, batch limit
    python manage.py crawl --limit 200
    python manage.py crawl --makes Toyota,Honda --years-back 5
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from scraper.crawler import discover_frontier, seed_crawl


class Command(BaseCommand):
    help = "Discover models (NHTSA) and enqueue background crawl jobs."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Max models to enqueue.")
        parser.add_argument("--makes", default="", help="Comma-separated makes (default: mainstream).")
        parser.add_argument("--years-back", type=int, default=None, help="How many recent years.")

    def handle(self, *args, **options):
        makes = [m.strip() for m in options["makes"].split(",") if m.strip()] or None
        years = None
        if options["years_back"]:
            from scraper.crawler import _years  # reuse current-year logic
            import django.utils.timezone as tz
            current = tz.now().year
            years = list(range(current - options["years_back"] + 1, current + 1))
        limit = options["limit"] or getattr(settings, "SCRAPER_CRAWL_BATCH", 50)

        self.stdout.write("Discovering models via NHTSA...")
        frontier = discover_frontier(makes, years)
        self.stdout.write(f"Frontier: {len(frontier)} model-years.")

        seeded = seed_crawl(frontier, limit)
        self.stdout.write(self.style.SUCCESS(
            f"Enqueued {seeded} new crawl job(s) (limit {limit})."
        ))
