"""Re-enqueue stale VehicleModels (past the cache TTL) for re-scraping.

One-shot version of the planner's refresh step. Schedule it with cron / Windows
Task Scheduler if you disable the background crawler.

Usage:
    python manage.py refresh_stale
    python manage.py refresh_stale --limit 200
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from scraper.crawler import refresh_stale


class Command(BaseCommand):
    help = "Re-enqueue VehicleModels past the cache TTL for re-scraping."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Max models to refresh.")

    def handle(self, *args, **options):
        limit = options["limit"] or getattr(settings, "SCRAPER_CRAWL_BATCH", 50)
        n = refresh_stale(limit)
        self.stdout.write(self.style.SUCCESS(f"Re-enqueued {n} stale model(s) (limit {limit})."))
