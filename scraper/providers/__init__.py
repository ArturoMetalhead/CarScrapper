"""Paquete de providers de scraping.

Un *provider* sabe cómo obtener y parsear la info de un vehículo desde una
fuente concreta. La mayoría de fuentes usan el `GenericProvider`, que se
configura por selectores CSS desde la base de datos (modelo `ScraperSource`).

Para un sitio que necesite lógica especial (por ejemplo, parsear JSON embebido
o llamar a una API interna), crea una subclase de `BaseProvider` y regístrala
con `@register("mi_clave")`. Luego pon ese `provider_key` en la fuente.
"""
from .base import BaseProvider, ScrapedVehicle
from .generic import GenericProvider, PlaywrightGenericProvider
from .registry import get_provider_class, register

# Importar aquí los providers personalizados hace que se registren al cargar
# el paquete. `edmunds` es el provider del sitio principal.
from . import edmunds  # noqa: F401  (efecto secundario: registra el provider)

__all__ = [
    "BaseProvider",
    "ScrapedVehicle",
    "GenericProvider",
    "PlaywrightGenericProvider",
    "get_provider_class",
    "register",
]
