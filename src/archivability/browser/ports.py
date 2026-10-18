from __future__ import annotations

from typing import Protocol

from archivability.browser.models import BrowserRenderRequest, BrowserRenderResult


class BrowserRenderer(Protocol):
    """Adapter boundary implemented only by an isolated browser worker."""

    def render(self, request: BrowserRenderRequest) -> BrowserRenderResult: ...

