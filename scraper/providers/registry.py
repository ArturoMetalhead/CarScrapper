"""Provider registry by key.

Lets sources (`ScraperSource.provider_key`) point to a concrete provider class.
If the key is not registered, `GenericProvider` is used.
"""
from __future__ import annotations

from typing import Type

from .base import BaseProvider

_PROVIDERS: dict[str, Type[BaseProvider]] = {}


def register(key: str):
    """Decorator to register a provider class under a key."""

    def decorator(cls: Type[BaseProvider]) -> Type[BaseProvider]:
        _PROVIDERS[key] = cls
        return cls

    return decorator


def get_provider_class(key: str) -> Type[BaseProvider]:
    """Return the provider class for the given key, or GenericProvider."""
    if key in _PROVIDERS:
        return _PROVIDERS[key]
    from .generic import GenericProvider

    return GenericProvider
