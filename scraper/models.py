"""Scraper models.

- `ScraperSource`: configurable sources (pages) to scrape vehicle info from.
  Tried in `priority` order; if the primary one fails the service falls back to
  the next active source automatically.
- `VehicleModel`: market data scraped per MODEL (make/model/year/trim). This is
  the cache filled by the background scraping; several VINs of the same model
  share this row.
- `ScrapeJob`: background scraping queue. A worker processes them one at a time
  (nodriver allows a single browser) and notifies via webhook when done.
- `Vehicle`: a specific VIN already resolved (decoded with NHTSA + linked to its
  `VehicleModel`). Serves repeated lookups of the same VIN instantly.
"""
from django.db import models


class ScraperSource(models.Model):
    """A configurable page/source to scrape vehicle info from.

    Sources are tried in `priority` order (lower = first). If one fails, the
    service moves on to the next active source automatically.
    """

    name = models.CharField("Name", max_length=100, unique=True)
    slug = models.SlugField("Slug", max_length=100, unique=True)

    base_url = models.URLField("Base URL", max_length=500)
    vin_path_template = models.CharField(
        "VIN path template",
        max_length=300,
        default="/inventory/vin/{vin}",
        help_text="Path appended to the base URL to query a VIN. Use {vin} as placeholder.",
    )
    model_path_template = models.CharField(
        "Model path template",
        max_length=300,
        blank=True,
        default="",
        help_text=(
            "Path to query by MODEL (used by the background scraping). "
            "Placeholders: {make} {model} {year} {trim}. e.g. /{make}/{model}/{year}/"
        ),
    )

    provider_key = models.CharField(
        "Provider key",
        max_length=50,
        default="generic",
        help_text=(
            "Provider class key. 'generic' uses the CSS selectors below; other "
            "values require a class registered in code."
        ),
    )

    selectors = models.JSONField(
        "CSS selectors",
        default=dict,
        blank=True,
        help_text=(
            "Map of field -> CSS selector. Fields: make, model, year, trim, "
            "mileage, estimated_price, currency, not_found, model_price_nodes, wait_for."
        ),
    )

    priority = models.PositiveIntegerField(
        "Priority", default=100, help_text="Lower number = tried first."
    )
    is_active = models.BooleanField("Active", default=True)
    timeout = models.PositiveIntegerField(
        "Timeout (s)",
        null=True,
        blank=True,
        help_text="Source-specific timeout. If empty, the global one is used.",
    )

    created_at = models.DateTimeField("Created", auto_now_add=True)
    updated_at = models.DateTimeField("Updated", auto_now=True)

    class Meta:
        verbose_name = "Scraper source"
        verbose_name_plural = "Scraper sources"
        ordering = ["priority", "name"]

    def __str__(self) -> str:
        state = "active" if self.is_active else "inactive"
        return f"{self.name} (priority {self.priority}, {state})"

    def build_url(self, vin: str) -> str:
        """Build the vehicle detail URL for a given VIN."""
        path = self.vin_path_template.format(vin=vin)
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def build_model_url(self, make: str, model: str, year=None, trim: str = "") -> str:
        """Build the model lookup URL (used by the background scraping).

        Values are normalized to lowercase and hyphens (common URL format, e.g.
        Edmunds: /honda/accord/2019/). Requires `model_path_template`.
        """
        if not self.model_path_template:
            raise ValueError(
                f"Source '{self.name}' has no model_path_template configured."
            )

        def slug(value) -> str:
            return "-".join(str(value or "").strip().lower().split())

        path = self.model_path_template.format(
            make=slug(make), model=slug(model), year=year or "", trim=slug(trim)
        )
        # Collapse double slashes left when year/trim are empty.
        path = "/".join(seg for seg in path.split("/") if seg)
        return f"{self.base_url.rstrip('/')}/{path}/"


class VehicleModel(models.Model):
    """Market data scraped per MODEL (make/model/year/trim).

    This is the cache filled by the background scraping. The key is the
    (make, model, year, trim) combination: several VINs of the same model share
    this row. `updated_at` tells whether the data is fresh.
    """

    make = models.CharField("Make", max_length=100)
    model = models.CharField("Model", max_length=100)
    year = models.PositiveIntegerField("Year", null=True, blank=True)
    trim = models.CharField("Trim", max_length=120, blank=True, default="")

    # Headline market price (Edmunds' suggested price for new cars, or the
    # median of used listings). `price_low`/`price_high` hold the range and
    # `price_kind` records where the number came from.
    estimated_price = models.DecimalField(
        "Estimated price", max_digits=12, decimal_places=2, null=True, blank=True
    )
    price_low = models.DecimalField(
        "Price (low)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    price_high = models.DecimalField(
        "Price (high)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    price_kind = models.CharField(
        "Price kind",
        max_length=32,
        blank=True,
        default="",
        help_text="How estimated_price was obtained: edmunds_suggested, msrp_range_mid, used_listings_median.",
    )
    currency = models.CharField("Currency", max_length=8, default="USD", blank=True)

    source = models.ForeignKey(
        ScraperSource,
        verbose_name="Source",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicle_models",
    )
    source_url = models.URLField("Source URL", max_length=500, blank=True)
    raw_data = models.JSONField("Raw data", default=dict, blank=True)

    created_at = models.DateTimeField("Created", auto_now_add=True)
    updated_at = models.DateTimeField("Updated", auto_now=True)

    class Meta:
        verbose_name = "Model data"
        verbose_name_plural = "Model data"
        ordering = ["make", "model", "year", "trim"]
        constraints = [
            models.UniqueConstraint(
                fields=["make", "model", "year", "trim"],
                name="uniq_vehiclemodel_make_model_year_trim",
            )
        ]

    def __str__(self) -> str:
        return " ".join(str(x) for x in (self.year, self.make, self.model, self.trim) if x)


class ScrapeJob(models.Model):
    """Background scraping job for a specific model.

    A worker processes them one at a time (nodriver allows a single browser) and
    notifies via webhook when done.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    make = models.CharField("Make", max_length=100)
    model = models.CharField("Model", max_length=100)
    year = models.PositiveIntegerField("Year", null=True, blank=True)
    trim = models.CharField("Trim", max_length=120, blank=True, default="")
    # NHTSA "Series" (e.g. BMW "3-Series") — an extra model-slug candidate for
    # sites (Edmunds) that group by series instead of engine variant.
    series = models.CharField("Series", max_length=120, blank=True, default="")

    # VIN that triggered the job (for the webhook). May come from a prewarm.
    vin = models.CharField("Origin VIN", max_length=17, blank=True, default="", db_index=True)

    status = models.CharField(
        "Status", max_length=10, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    # Lower number = processed first. On-demand lookups use a low value so they
    # jump ahead of background crawl/refresh jobs.
    priority = models.PositiveIntegerField("Priority", default=100, db_index=True)
    origin = models.CharField(
        "Origin", max_length=20, blank=True, default="lookup",
        help_text="Who created the job: lookup, model_lookup, prewarm, crawl, refresh.",
    )
    attempts = models.PositiveIntegerField("Attempts", default=0)
    last_error = models.TextField("Last error", blank=True, default="")

    # Optional callback URL; if empty, SCRAPER_WEBHOOK_URL is used.
    webhook_url = models.URLField("Webhook", max_length=500, blank=True, default="")

    result = models.ForeignKey(
        VehicleModel,
        verbose_name="Result",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )

    created_at = models.DateTimeField("Created", auto_now_add=True)
    started_at = models.DateTimeField("Started", null=True, blank=True)
    finished_at = models.DateTimeField("Finished", null=True, blank=True)

    class Meta:
        verbose_name = "Scrape job"
        verbose_name_plural = "Scrape jobs"
        ordering = ["priority", "created_at"]
        indexes = [models.Index(fields=["status", "priority", "created_at"])]

    def __str__(self) -> str:
        target = " ".join(str(x) for x in (self.year, self.make, self.model, self.trim) if x)
        return f"[{self.status}] {target or self.vin}"


class Vehicle(models.Model):
    """A specific resolved VIN: decoded data (NHTSA) + its model.

    Caches the result per VIN so repeated lookups of the same VIN are instant
    without decoding again. The price/market data is inherited from the linked
    `VehicleModel` (scraped in the background).
    """

    vin = models.CharField(
        "VIN",
        max_length=17,
        unique=True,
        db_index=True,
        help_text="Vehicle Identification Number (17 characters).",
    )

    # Decoded VIN data (NHTSA).
    make = models.CharField("Make", max_length=100, blank=True)
    model = models.CharField("Model", max_length=100, blank=True)
    year = models.PositiveIntegerField("Year", null=True, blank=True)
    trim = models.CharField("Trim", max_length=120, blank=True)
    body_class = models.CharField("Body class", max_length=100, blank=True, default="")
    mileage = models.PositiveIntegerField("Mileage", null=True, blank=True)

    # Market data, inherited from the scraped model.
    vehicle_model = models.ForeignKey(
        VehicleModel,
        verbose_name="Model data",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicles",
    )
    estimated_price = models.DecimalField(
        "Estimated price", max_digits=12, decimal_places=2, null=True, blank=True
    )
    currency = models.CharField("Currency", max_length=8, default="USD", blank=True)

    source = models.ForeignKey(
        ScraperSource,
        verbose_name="Source",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicles",
    )
    source_url = models.URLField("Source URL", max_length=500, blank=True)
    raw_data = models.JSONField(
        "Raw data",
        default=dict,
        blank=True,
        help_text="NHTSA decode and scraping payload, in case it is needed later.",
    )

    created_at = models.DateTimeField("Created", auto_now_add=True)
    updated_at = models.DateTimeField("Updated", auto_now=True)

    class Meta:
        verbose_name = "Vehicle"
        verbose_name_plural = "Vehicles"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        label = " ".join(str(x) for x in (self.year, self.make, self.model) if x)
        return f"{self.vin} ({label})" if label else self.vin
