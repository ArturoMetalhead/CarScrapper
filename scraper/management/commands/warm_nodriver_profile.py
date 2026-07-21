"""Warm up / verify the Chrome profile used by nodriver against Edmunds.

DataDome treats a "returning" profile better (with a datadome cookie and
accumulated IP trust). This command launches the real browser against an Edmunds
page and reports whether it gets past the block, leaving the profile seeded for
later scrapes.

Usage:
    python manage.py warm_nodriver_profile
    python manage.py warm_nodriver_profile --url https://www.edmunds.com/used-all/
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from scraper.models import ScraperSource
from scraper.providers.base import BaseProvider, ScraperError
from scraper.providers.nodriver_fetch import NodriverFetchMixin


class _Probe(NodriverFetchMixin, BaseProvider):
    """Minimal provider: only uses nodriver's fetch to test a URL."""

    def parse(self, response, vin):  # unused here
        raise NotImplementedError


class Command(BaseCommand):
    help = "Warm up and verify the Chrome profile (nodriver) against Edmunds."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default="https://www.edmunds.com/used-all/",
            help="URL to test (defaults to a DataDome-protected Edmunds page).",
        )

    def handle(self, *args, **options):
        url = options["url"]
        source = ScraperSource(
            name="warm-up",
            slug="warm-up",
            base_url=url,
            vin_path_template="",
            provider_key="nodriver",
            selectors={},
        )
        probe = _Probe(source)

        self.stdout.write(f"Testing {url} with nodriver (persistent profile)...")
        try:
            resp = probe._render(url)
        except ScraperError as exc:
            raise SystemExit(self.style.ERROR(f"Browser failure: {exc}"))

        size = len(resp.text)
        if resp.ok and size > 100_000:
            self.stdout.write(self.style.SUCCESS(
                f"OK: bypass achieved (status {resp.status_code}, {size} chars). "
                "Profile warmed and ready."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"BLOCKED or insufficient content (status {resp.status_code}, {size} chars). "
                "Retry the command; a 403 seeds the cookie that lets the next attempt pass."
            ))
