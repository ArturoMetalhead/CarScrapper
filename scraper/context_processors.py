"""Template context processors for the scraper app."""
from django.conf import settings
from django.utils.functional import SimpleLazyObject


def vinaudit(request):
    """Expose the paid-VinAudit UI state to templates:
      * vinaudit_enabled — whether the paid button should be shown (key set AND an
        active source). Wrapped in SimpleLazyObject so the ScraperSource DB query only
        runs if a template actually reads the flag (the dashboard) — not on every
        admin/DRF/404 render, which don't reference it.
      * vinaudit_cost_notice — the configurable cost warning (button + confirm dialog).
    """
    from scraper import services  # lazy: avoid import at app-load time

    return {
        "vinaudit_enabled": SimpleLazyObject(services.vinaudit_enabled),
        "vinaudit_cost_notice": getattr(settings, "VINAUDIT_COST_NOTICE", ""),
    }
