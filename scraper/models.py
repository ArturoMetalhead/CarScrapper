"""Modelos del scraper.

- `ScraperSource`: fuentes configurables (páginas) desde las que se obtiene la
  info del vehículo. Se ordenan por prioridad y se pueden activar/desactivar.
  Si la fuente principal falla, el servicio prueba la siguiente automáticamente.
- `Vehicle`: información de un vehículo obtenida por scraping a partir de su VIN.
  Guardarla permite servir consultas repetidas desde caché.
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


class Vehicle(models.Model):
    """Información de un vehículo obtenida por scraping a partir de su VIN."""

    vin = models.CharField(
        "VIN",
        max_length=17,
        unique=True,
        db_index=True,
        help_text="Número de identificación del vehículo (17 caracteres).",
    )

    # Datos básicos del vehículo.
    make = models.CharField("Marca", max_length=100, blank=True)
    model = models.CharField("Modelo", max_length=100, blank=True)
    year = models.PositiveIntegerField("Año", null=True, blank=True)
    trim = models.CharField("Versión", max_length=100, blank=True)
    mileage = models.PositiveIntegerField("Kilometraje", null=True, blank=True)

    # El dato principal: el precio estimado que muestra la página scrapeada.
    estimated_price = models.DecimalField(
        "Precio estimado",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    currency = models.CharField("Moneda", max_length=8, default="USD", blank=True)

    # Trazabilidad del scraping: de qué fuente y URL salió el dato.
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
        help_text="Payload completo extraído del sitio, por si hace falta después.",
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
