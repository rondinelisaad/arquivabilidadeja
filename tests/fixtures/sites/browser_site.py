from __future__ import annotations

from dataclasses import dataclass

from tests.fixtures.sites.controlled_site import ControlledSite, FixtureResponse


@dataclass(slots=True)
class BrowserFixtureSite:
    """Controlled SPA/API fixture with internal DOM gain and an external API origin."""

    site: ControlledSite

    @classmethod
    def create(cls) -> BrowserFixtureSite:
        site = ControlledSite({})
        external_api = site.url("/api/content", hostname="other.test")
        script = f"""
fetch({external_api!r})
  .then(response => response.json())
  .then(data => {{
    const link = document.createElement('a');
    link.href = data.href;
    link.textContent = data.label;
    document.querySelector('#app').appendChild(link);
  }});
""".strip()
        site.set_route(
            "/spa",
            FixtureResponse(
                content_type="text/html; charset=utf-8",
                body=(
                    "<!doctype html><html><body><main id='app'></main>"
                    "<script src='/app.js'></script></body></html>"
                ).encode(),
            ),
        )
        site.set_route(
            "/app.js",
            FixtureResponse(
                content_type="application/javascript",
                body=script.encode(),
            ),
        )
        site.set_route(
            "/api/content",
            FixtureResponse(
                content_type="application/json",
                body=b'{"href":"/rendered","label":"Rendered content"}',
            ),
        )
        site.set_route(
            "/rendered",
            FixtureResponse(content_type="text/html", body=b"rendered target"),
        )
        return cls(site)

    def __enter__(self) -> BrowserFixtureSite:
        self.site.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.site.__exit__(exc_type, exc, traceback)
