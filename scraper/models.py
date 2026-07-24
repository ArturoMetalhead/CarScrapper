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

Los `verbose_name` visibles están en español (el proyecto usa LANGUAGE_CODE=es);
los valores internos (choices, keys) se mantienen en inglés a propósito.
"""
from django.db import models


class ScraperSource(models.Model):
    """A configurable page/source to scrape vehicle info from.

    Sources are tried in `priority` order (lower = first). If one fails, the
    service moves on to the next active source automatically.
    """

    name = models.CharField("Nombre", max_length=100, unique=True)
    slug = models.SlugField("Slug", max_length=100, unique=True)

    base_url = models.URLField("URL base", max_length=500)
    vin_path_template = models.CharField(
        "Plantilla de ruta VIN",
        max_length=300,
        default="/inventory/vin/{vin}",
        help_text="Path appended to the base URL to query a VIN. Use {vin} as placeholder.",
    )
    model_path_template = models.CharField(
        "Plantilla de ruta de modelo",
        max_length=300,
        blank=True,
        default="",
        help_text=(
            "Path to query by MODEL (used by the background scraping). "
            "Placeholders: {make} {model} {year} {trim}. e.g. /{make}/{model}/{year}/"
        ),
    )

    provider_key = models.CharField(
        "Clave de proveedor",
        max_length=50,
        default="generic",
        help_text=(
            "Provider class key. 'generic' uses the CSS selectors below; other "
            "values require a class registered in code."
        ),
    )

    selectors = models.JSONField(
        "Selectores CSS",
        default=dict,
        blank=True,
        help_text=(
            "Map of field -> CSS selector. Fields: make, model, year, trim, "
            "mileage, estimated_price, currency, not_found, model_price_nodes, wait_for."
        ),
    )

    priority = models.PositiveIntegerField(
        "Prioridad", default=100, help_text="Lower number = tried first."
    )
    is_active = models.BooleanField("Activa", default=True)
    timeout = models.PositiveIntegerField(
        "Tiempo límite (s)",
        null=True,
        blank=True,
        help_text="Source-specific timeout. If empty, the global one is used.",
    )

    created_at = models.DateTimeField("Creado", auto_now_add=True)
    updated_at = models.DateTimeField("Actualizado", auto_now=True)

    class Meta:
        verbose_name = "Fuente de scraping"
        verbose_name_plural = "Fuentes de scraping"
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

    make = models.CharField("Marca", max_length=100)
    model = models.CharField("Modelo", max_length=100)
    year = models.PositiveIntegerField("Año", null=True, blank=True)
    trim = models.CharField("Versión", max_length=120, blank=True, default="")

    # Headline market price (Edmunds' suggested price for new cars, or the
    # median of used listings). `price_low`/`price_high` hold the range and
    # `price_kind` records where the number came from.
    estimated_price = models.DecimalField(
        "Precio estimado", max_digits=12, decimal_places=2, null=True, blank=True
    )
    price_low = models.DecimalField(
        "Precio (mín)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    price_high = models.DecimalField(
        "Precio (máx)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    price_kind = models.CharField(
        "Tipo de precio",
        max_length=32,
        blank=True,
        default="",
        help_text="How estimated_price was obtained: edmunds_suggested, msrp_range_mid, used_listings_median.",
    )
    currency = models.CharField("Moneda", max_length=8, default="USD", blank=True)

    source = models.ForeignKey(
        ScraperSource,
        verbose_name="Fuente",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicle_models",
    )
    source_url = models.URLField("URL de la fuente", max_length=500, blank=True)
    raw_data = models.JSONField("Datos crudos", default=dict, blank=True)

    created_at = models.DateTimeField("Creado", auto_now_add=True)
    updated_at = models.DateTimeField("Actualizado", auto_now=True)

    class Meta:
        verbose_name = "Datos de modelo"
        verbose_name_plural = "Datos de modelos"
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
        PENDING = "pending", "Pendiente"
        RUNNING = "running", "En curso"
        DONE = "done", "Listo"
        FAILED = "failed", "Falló"

    make = models.CharField("Marca", max_length=100)
    model = models.CharField("Modelo", max_length=100)
    year = models.PositiveIntegerField("Año", null=True, blank=True)
    trim = models.CharField("Versión", max_length=120, blank=True, default="")
    # NHTSA "Series" (e.g. BMW "3-Series") — an extra model-slug candidate for
    # sites (Edmunds) that group by series instead of engine variant.
    series = models.CharField("Serie", max_length=120, blank=True, default="")

    # VIN that triggered the job (for the webhook). May come from a prewarm.
    vin = models.CharField("VIN de origen", max_length=17, blank=True, default="", db_index=True)

    status = models.CharField(
        "Estado", max_length=10, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    # Lower number = processed first. On-demand lookups use a low value so they
    # jump ahead of background crawl/refresh jobs.
    priority = models.PositiveIntegerField("Prioridad", default=100, db_index=True)
    origin = models.CharField(
        "Origen", max_length=20, blank=True, default="lookup",
        help_text="Who created the job: lookup, model_lookup, prewarm, crawl, refresh, rescrape.",
    )
    attempts = models.PositiveIntegerField("Intentos", default=0)
    last_error = models.TextField("Último error", blank=True, default="")

    # Optional callback URL; if empty, SCRAPER_WEBHOOK_URL is used.
    webhook_url = models.URLField("Webhook", max_length=500, blank=True, default="")

    result = models.ForeignKey(
        VehicleModel,
        verbose_name="Resultado",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )

    created_at = models.DateTimeField("Creado", auto_now_add=True)
    started_at = models.DateTimeField("Iniciado", null=True, blank=True)
    finished_at = models.DateTimeField("Terminado", null=True, blank=True)

    class Meta:
        verbose_name = "Trabajo de scraping"
        verbose_name_plural = "Trabajos de scraping"
        ordering = ["priority", "created_at"]
        indexes = [models.Index(fields=["status", "priority", "created_at"])]
        constraints = [
            # At most one ACTIVE (pending/running) job per make/model/year/trim, so
            # concurrent requests can't create duplicate scrapes. (A NULL year or a
            # different make casing is not covered by this partial index and would
            # fall back to a harmless redundant scrape.)
            models.UniqueConstraint(
                fields=["make", "model", "year", "trim"],
                condition=models.Q(status__in=["pending", "running"]),
                name="uniq_active_scrapejob",
            ),
        ]

    def __str__(self) -> str:
        target = " ".join(str(x) for x in (self.year, self.make, self.model, self.trim) if x)
        return f"[{self.status}] {target or self.vin}"


class ScrapeSubscriber(models.Model):
    """A caller waiting on a ScrapeJob's result.

    Because concurrent requests for the same model are deduped onto ONE job, each
    distinct caller (its own VIN + webhook) registers here, so every requester is
    notified with their own VIN — not just whoever created the job.
    """

    job = models.ForeignKey(
        ScrapeJob, verbose_name="Trabajo", on_delete=models.CASCADE, related_name="subscribers"
    )
    vin = models.CharField("VIN", max_length=17, blank=True, default="")
    webhook_url = models.URLField("Webhook", max_length=500, blank=True, default="")
    notified = models.BooleanField("Notificado", default=False)
    created_at = models.DateTimeField("Creado", auto_now_add=True)

    class Meta:
        verbose_name = "Suscriptor"
        verbose_name_plural = "Suscriptores"
        constraints = [
            models.UniqueConstraint(
                fields=["job", "vin", "webhook_url"], name="uniq_job_subscriber"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vin or '?'} -> {self.webhook_url or '(default webhook)'}"


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
    make = models.CharField("Marca", max_length=100, blank=True)
    model = models.CharField("Modelo", max_length=100, blank=True)
    year = models.PositiveIntegerField("Año", null=True, blank=True)
    trim = models.CharField("Versión", max_length=120, blank=True)
    body_class = models.CharField("Carrocería", max_length=100, blank=True, default="")
    mileage = models.PositiveIntegerField("Kilometraje", null=True, blank=True)

    # Market data, inherited from the scraped model.
    vehicle_model = models.ForeignKey(
        VehicleModel,
        verbose_name="Datos de modelo",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicles",
    )
    estimated_price = models.DecimalField(
        "Precio estimado", max_digits=12, decimal_places=2, null=True, blank=True
    )
    currency = models.CharField("Moneda", max_length=8, default="USD", blank=True)

    source = models.ForeignKey(
        ScraperSource,
        verbose_name="Fuente",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicles",
    )
    source_url = models.URLField("URL de la fuente", max_length=500, blank=True)
    raw_data = models.JSONField(
        "Datos crudos",
        default=dict,
        blank=True,
        help_text="NHTSA decode and scraping payload, in case it is needed later.",
    )

    created_at = models.DateTimeField("Creado", auto_now_add=True)
    updated_at = models.DateTimeField("Actualizado", auto_now=True)

    class Meta:
        verbose_name = "Vehículo"
        verbose_name_plural = "Vehículos"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        label = " ".join(str(x) for x in (self.year, self.make, self.model) if x)
        return f"{self.vin} ({label})" if label else self.vin
