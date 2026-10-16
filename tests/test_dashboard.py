"""Smoke test: every dashboard page renders without an exception for three different scopes.

The API runs in its own process against the test database, and the dashboard pages
run through Streamlit's AppTest, so this exercises the same HTTP path a browser does.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import FPA_LEAD, PLATFORM_MGR, VP_ENG

PAGES = ["views/variances.py", "views/headcount.py", "views/forecast.py", "views/ask.py", "views/definitions.py",
         "views/data_status.py"]


@pytest.fixture(scope="module")
def api_url(published):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    # A separate process, so its token issuer and pool never replace the ones the other tests share.
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "fpa.api:app", "--host", "127.0.0.1",
                               "--port", str(port), "--log-level", "warning"])
    url = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    os.environ["FPA_API_URL"] = url
    sys.modules.pop("common", None)
    yield url
    server.terminate()
    server.wait(timeout=10)


@pytest.mark.parametrize("person", [VP_ENG, FPA_LEAD, PLATFORM_MGR])
def test_every_page_renders(api_url, person):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("../dashboard/app.py", default_timeout=90)
    at.query_params["as"] = person
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Budget vs actual, September 2026"
    for page in PAGES:
        at.switch_page(page).run()
        assert not at.exception, (page, at.exception)
        assert at.title, page
