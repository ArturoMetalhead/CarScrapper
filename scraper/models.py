"""Modelos del scraper.

- `ScraperSource`: fuentes configurables (páginas) desde las que se obtiene la
  info del vehículo. Se ordenan por prioridad y se pueden activar/desactivar.
  Si la fuente principal falla, el servicio prueba la siguiente automáticamente.
- `VehicleModel`: datos de mercado scrapeados por MODELO (marca/modelo/año/trim).
  Es el caché que llena el scraping en segundo plano; varios VINs del mismo
  modelo comparten esta información.
- `ScrapeJob`: cola de trabajos de scraping en segundo plano. Un worker los va
  procesando (uno a uno, por la restricción de nodriver) y notifica por webhook.
- `Vehicle`: un VIN concreto ya resuelto (decodificado con NHTSA + enlazado a su
  `VehicleModel`). Sirve consultas repetidas del mismo VIN al instante.
"""
from django.db import models


class ScraperSource(models.Model):
    """Una página/fuente configurable de la que scrapear info de vehículos.

    Las fuentes se prueban en orden de `priority` (menor = primero). Si una
    falla, el servicio pasa automáticamente a la siguiente activa.
    """

    name = models.CharField("Nombre", max_length=100, unique=True)
    slug = models.SlugField("Identificador", max_length=100, unique=True)

    base_url = models.URLField("URL base", max_length=500)
    vin_path_template = models.CharField(
        "Plantilla de ruta por VIN",
        max_length=300,
        default="/inventory/vin/{vin}",
        help_text=(
            "Ruta que se agrega a la URL base para consultar un VIN. "
            "Usa {vin} como marcador. Ej: /inventory/vin/{vin}"
        ),
    )
    model_path_template = models.CharField(
        "Plantilla de ruta por modelo",
        max_length=300,
        blank=True,
        default="",
        help_text=(
            "Ruta para consultar por MODELO (el scraping en segundo plano usa "
            "esta). Marcadores: {make} {model} {year} {trim}. "
            "Ej: /{make}/{model}/{year}/"
        ),
    )

    provider_key = models.CharField(
        "Proveedor (código)",
        max_length=50,
        default="generic",
        help_text=(
            "Clave del provider a usar. 'generic' usa los selectores CSS de "
            "abajo. Otros valores requieren una clase registrada en el código."
        ),
    )

    selectors = models.JSONField(
        "Selectores CSS",
        default=dict,
        blank=True,
        help_text=(
            "Mapa campo -> selector CSS. Campos: make, model, year, trim, "
            "mileage, estimated_price, currency, not_found. "
            'Ej: {"estimated_price": ".price-display"}'
        ),
    )

    priority = models.PositiveIntegerField(
        "Prioridad",
        default=100,
        help_text="Menor número = se intenta primero.",
    )
    is_active = models.BooleanField("Activa", default=True)
    timeout = models.PositiveIntegerField(
        "Timeout (s)",
        null=True,
        blank=True,
        help_text="Timeout específico para esta fuente. Si se deja vacío, usa el global.",
    )

    created_at = models.DateTimeField("Creada", auto_now_add=True)
    updated_at = models.DateTimeField("Actualizada", auto_now=True)

    class Meta:
        verbose_name = "Fuente de scraping"
        verbose_name_plural = "Fuentes de scraping"
        ordering = ["priority", "name"]

    def __str__(self) -> str:
        estado = "activa" if self.is_active else "inactiva"
        return f"{self.name} (prioridad {self.priority}, {estado})"

    def build_url(self, vin: str) -> str:
        """Construye la URL de la ficha del vehículo para un VIN dado."""
        ruta = self.vin_path_template.format(vin=vin)
        return f"{self.base_url.rstrip('/')}/{ruta.lstrip('/')}"

    def build_model_url(self, make: str, model: str, year=None, trim: str = "") -> str:
        """Construye la URL de consulta por modelo (para el scraping de fondo).

        Normaliza los valores a minúsculas y guiones (formato habitual de URL,
        p. ej. Edmunds: /honda/accord/2019/). Requiere `model_path_template`.
        """
        if not self.model_path_template:
            raise ValueError(
                f"La fuente '{self.name}' no tiene model_path_template configurada."
            )

        def slug(valor) -> str:
            texto = str(valor or "").strip().lower()
            return "-".join(texto.split())

        ruta = self.model_path_template.format(
            make=slug(make), model=slug(model),
            year=year or "", trim=slug(trim),
        )
        # Colapsa dobles barras que puedan quedar si year/trim van vacíos.
        ruta = "/".join(seg for seg in ruta.split("/") if seg)
        return f"{self.base_url.rstrip('/')}/{ruta}/"


class VehicleModel(models.Model):
    """Datos de mercado scrapeados por MODELO (marca/modelo/año/versión).

    Es el caché que llena el scraping en segundo plano. La clave es la
    combinación (make, model, year, trim): varios VINs del mismo modelo
    comparten esta fila. `updated_at` permite saber si el dato está fresco.
    """

    make = models.CharField("Marca", max_length=100)
    model = models.CharField("Modelo", max_length=100)
    year = models.PositiveIntegerField("Año", null=True, blank=True)
    trim = models.CharField("Versión", max_length=120, blank=True, default="")

    # El dato principal: el precio estimado de mercado para el modelo.
    estimated_price = models.DecimalField(
        "Precio estimado",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    currency = models.CharField("Moneda", max_length=8, default="USD", blank=True)

    # Trazabilidad: de qué fuente y URL salió el dato.
    source = models.ForeignKey(
        ScraperSource,
        verbose_name="Fuente",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicle_models",
    )
    source_url = models.URLField("URL de origen", max_length=500, blank=True)
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
    """Trabajo de scraping en segundo plano para un modelo concreto.

    Un worker (management command) los procesa de uno en uno por la restricción
    de nodriver (un navegador a la vez) y, al terminar, avisa por webhook.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        RUNNING = "running", "En proceso"
        DONE = "done", "Completado"
        FAILED = "failed", "Fallido"

    # Modelo objetivo a scrapear.
    make = models.CharField("Marca", max_length=100)
    model = models.CharField("Modelo", max_length=100)
    year = models.PositiveIntegerField("Año", null=True, blank=True)
    trim = models.CharField("Versión", max_length=120, blank=True, default="")

    # VIN que originó el trabajo (para el webhook). Puede venir de una precarga.
    vin = models.CharField("VIN origen", max_length=17, blank=True, default="", db_index=True)

    status = models.CharField(
        "Estado",
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField("Intentos", default=0)
    last_error = models.TextField("Último error", blank=True, default="")

    # URL de callback opcional; si está vacía se usa SCRAPER_WEBHOOK_URL.
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
    finished_at = models.DateTimeField("Finalizado", null=True, blank=True)

    class Meta:
        verbose_name = "Trabajo de scraping"
        verbose_name_plural = "Trabajos de scraping"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:
        objetivo = " ".join(str(x) for x in (self.year, self.make, self.model, self.trim) if x)
        return f"[{self.status}] {objetivo or self.vin}"


class Vehicle(models.Model):
    """Un VIN concreto ya resuelto: datos decodificados (NHTSA) + su modelo.

    Cachea el resultado por VIN para que consultas repetidas del mismo VIN sean
    instantáneas sin volver a decodificar. El precio/mercado se hereda del
    `VehicleModel` enlazado (scrapeado en segundo plano).
    """

    vin = models.CharField(
        "VIN",
        max_length=17,
        unique=True,
        db_index=True,
        help_text="Número de identificación del vehículo (17 caracteres).",
    )

    # Datos decodificados del VIN (NHTSA).
    make = models.CharField("Marca", max_length=100, blank=True)
    model = models.CharField("Modelo", max_length=100, blank=True)
    year = models.PositiveIntegerField("Año", null=True, blank=True)
    trim = models.CharField("Versión", max_length=120, blank=True)
    body_class = models.CharField("Carrocería", max_length=100, blank=True, default="")
    mileage = models.PositiveIntegerField("Kilometraje", null=True, blank=True)

    # Datos de mercado, heredados del modelo scrapeado.
    vehicle_model = models.ForeignKey(
        VehicleModel,
        verbose_name="Datos de modelo",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicles",
    )
    estimated_price = models.DecimalField(
        "Precio estimado",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    currency = models.CharField("Moneda", max_length=8, default="USD", blank=True)

    # Trazabilidad.
    source = models.ForeignKey(
        ScraperSource,
        verbose_name="Fuente",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicles",
    )
    source_url = models.URLField("URL de origen", max_length=500, blank=True)
    raw_data = models.JSONField(
        "Datos crudos",
        default=dict,
        blank=True,
        help_text="Decodificación NHTSA y payload del scraping, por si hace falta después.",
    )

    created_at = models.DateTimeField("Creado", auto_now_add=True)
    updated_at = models.DateTimeField("Actualizado", auto_now=True)

    class Meta:
        verbose_name = "Vehículo"
        verbose_name_plural = "Vehículos"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        etiqueta = " ".join(str(x) for x in (self.year, self.make, self.model) if x)
        return f"{self.vin} ({etiqueta})" if etiqueta else self.vin
