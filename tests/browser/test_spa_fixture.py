from __future__ import annotations

import unittest
from urllib.request import urlopen

from tests.fixtures.sites import BrowserFixtureSite


class BrowserSpaFixtureTests(unittest.TestCase):
    def test_fixture_separates_raw_html_script_external_api_and_rendered_link(self) -> None:
        with BrowserFixtureSite.create() as fixture:
            base = f"http://127.0.0.1:{fixture.site.port}"
            with urlopen(f"{base}/spa", timeout=1) as response:  # noqa: S310
                html = response.read().decode()
            with urlopen(f"{base}/app.js", timeout=1) as response:  # noqa: S310
                script = response.read().decode()
            with urlopen(f"{base}/api/content", timeout=1) as response:  # noqa: S310
                api = response.read().decode()

        self.assertIn("id='app'", html)
        self.assertNotIn("/rendered", html)
        self.assertIn("other.test", script)
        self.assertIn("fetch(", script)
        self.assertIn("/rendered", api)


if __name__ == "__main__":
    unittest.main()
