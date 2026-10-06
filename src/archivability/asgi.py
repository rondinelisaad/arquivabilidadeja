from __future__ import annotations

from archivability.application.production import (
    ProductionAsgiApplication,
    ProductionSettings,
    create_production_app,
)


def create_app() -> ProductionAsgiApplication:
    """ASGI server factory; reads validated runtime settings without running DDL."""
    return create_production_app(ProductionSettings.from_environment())
