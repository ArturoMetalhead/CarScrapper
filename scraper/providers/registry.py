"""Registro de providers por clave.

Permite que las fuentes (`ScraperSource.provider_key`) apunten a una clase de
provider concreta. Si la clave no está registrada, se usa el `GenericProvider`.
"""
from __future__ import annotations

from typing import Type

from .base import BaseProvider

_PROVIDERS: dict[str, Type[BaseProvider]] = {}


def register(key: str):
    """Decorador para registrar una clase de provider bajo una clave."""

    def decorador(cls: Type[BaseProvider]) -> Type[BaseProvider]:
        _PROVIDERS[key] = cls
        return cls

    return decorador


def get_provider_class(key: str) -> Type[BaseProvider]:
    """Devuelve la clase de provider para la clave dada.

    Si la clave no está registrada, cae al GenericProvider (import diferido
    para evitar imports circulares).
    """
    if key in _PROVIDERS:
        return _PROVIDERS[key]
    from .generic import GenericProvider

    return GenericProvider
