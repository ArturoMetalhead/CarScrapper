"""Precarga fuentes de scraping por defecto.

Uso:
    python manage.py seed_sources

Es idempotente: si una fuente ya existe (por slug), la actualiza. Ajusta las
plantillas de URL y los selectores según la estructura real de cada sitio.
"""
from django.core.management.base import BaseCommand

from scraper.models import ScraperSource

# Edmunds es la fuente principal (prioridad más baja = se intenta primero).
# Las demás son respaldos: si Edmunds falla o desaparece, el sistema las usa
# automáticamente. Edita/añade fuentes aquí o directamente desde el admin.
FUENTES = [
    {
        "slug": "edmunds",
        "name": "Edmunds",
        "base_url": "https://www.edmunds.com",
        "vin_path_template": "/inventory/vin/{vin}/",
        # El scraping en segundo plano usa la URL por MODELO (marca/modelo/año).
        "model_path_template": "/{make}/{model}/{year}/",
        "provider_key": "edmunds",
        "priority": 10,
        "is_active": True,
        "selectors": {
            # Nodos de precio de los listados; se toma la mediana como precio de
            # mercado del modelo/año. Verificado contra el HTML real de Edmunds.
            "model_price_nodes": ".heading-3",
        },
    },
    {
        "slug": "fuente-respaldo",
        "name": "Fuente de respaldo (ejemplo)",
        "base_url": "https://example.com",
        "vin_path_template": "/vehicle/{vin}",
        "provider_key": "generic",
        "priority": 100,
        "is_active": False,  # desactivada hasta que la configures
        "selectors": {
            "estimated_price": ".estimated-price",
            "make": ".make",
            "model": ".model",
            "year": ".year",
            "not_found": ".no-results",
        },
    },
]


class Command(BaseCommand):
    help = "Precarga las fuentes de scraping por defecto (Edmunds + respaldo)."

    def handle(self, *args, **options):
        for datos in FUENTES:
            slug = datos.pop("slug")
            fuente, creada = ScraperSource.objects.update_or_create(
                slug=slug, defaults=datos
            )
            verbo = "Creada" if creada else "Actualizada"
            self.stdout.write(
                self.style.SUCCESS(f"{verbo}: {fuente.name} (prioridad {fuente.priority})")
            )
        self.stdout.write(self.style.SUCCESS("Fuentes precargadas."))
