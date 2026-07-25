"""
Test suite for Page Pulse's FastAPI backend (main.py).

Runs entirely offline: every test patches `main.AsyncSession` (curl_cffi's
async client) so no real HTTP request ever leaves the machine. This keeps
the suite fast and deterministic — results don't depend on any live site
being up, slow, or blocking us.

Run with:
    pytest test_main.py -v
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

class FakeResponse:
    """Minimal stand-in for a curl_cffi Response — only the attributes
    main.py actually touches: .status_code, .headers, .text, .url
    """

    def __init__(self, status_code=200, headers=None, text="", url="https://example.com/"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = url


def build_mock_session(response=None, exception=None):
    """Build a mock that behaves like `async with AsyncSession(...) as session`.

    Pass `response=` for a mock that returns that FakeResponse from .get().
    Pass `exception=` for a mock whose .get() raises that exception instead
    (simulates DNS failures, connection refused, timeouts, etc).
    """
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    if exception is not None:
        mock_session.get = AsyncMock(side_effect=exception)
    else:
        mock_session.get = AsyncMock(return_value=response)

    return mock_session


# Deliberately simple HTML so every expected metric below can be counted
# by hand rather than re-derived from main.py's own parsing logic.
#
#   title              -> "Example Domain"
#   meta description   -> "This domain is for use in illustrative examples."
#   h1 count            -> 1  ("Welcome")
#   images missing alt  -> 2  (banner.png has no alt, icon.png has alt="")
#   word count           -> 13 (see breakdown below)
#
# Visible text after script removal: title + h1 + paragraph text
#   "Example Domain Welcome This is a simple test page with five words here."
#   Example(1) Domain(2) Welcome(3) This(4) is(5) a(6) simple(7) test(8)
#   page(9) with(10) five(11) words(12) here(13)  -> 13 words
SAMPLE_HTML = """
<html>
<head>
    <title>Example Domain</title>
    <meta name="description" content="This domain is for use in illustrative examples.">
</head>
<body>
    <h1>Welcome</h1>
    <p>This is a simple test page with five words here.</p>
    <img src="logo.png" alt="Site logo">
    <img src="banner.png">
    <img src="icon.png" alt="">
    <script>console.log("ignored script content");</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

def test_happy_path_returns_full_metrics():
    fake_response = FakeResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        text=SAMPLE_HTML,
        url="https://example.com/",
    )

    with patch("main.AsyncSession") as mock_session_cls:
        mock_session_cls.return_value = build_mock_session(response=fake_response)
        resp = client.post("/api/analyze", json={"url": "https://example.com"})

    assert resp.status_code == 200
    data = resp.json()

    assert data["success"] is True
    assert data["url"] == "https://example.com/"
    assert data["status_code"] == 200
    assert isinstance(data["response_time_ms"], int)
    assert data["response_time_ms"] >= 0
    assert data["title"] == "Example Domain"
    assert data["meta_description"] == "This domain is for use in illustrative examples."
    assert data["h1_count"] == 1
    assert data["images_missing_alt"] == 2
    assert data["word_count"] == 13
    assert data["error"] is None


def test_happy_path_handles_missing_title_and_meta_gracefully():
    """A bare-bones page with no <title> or <meta name="description"> should
    still succeed, just with those fields as null rather than crashing."""
    bare_html = "<html><body><h1>Just a heading</h1></body></html>"
    fake_response = FakeResponse(
        status_code=200,
        headers={"content-type": "text/html"},
        text=bare_html,
        url="https://bare.example/",
    )

    with patch("main.AsyncSession") as mock_session_cls:
        mock_session_cls.return_value = build_mock_session(response=fake_response)
        resp = client.post("/api/analyze", json={"url": "https://bare.example"})

    data = resp.json()
    assert data["success"] is True
    assert data["title"] is None
    assert data["meta_description"] is None
    assert data["h1_count"] == 1


# ---------------------------------------------------------------------------
# 2. Failure case: invalid / non-existent domain
# ---------------------------------------------------------------------------

def test_nonexistent_domain_returns_structured_error_not_a_crash():
    dns_failure = Exception("Could not resolve host: this-domain-does-not-exist.invalid")

    with patch("main.AsyncSession") as mock_session_cls:
        mock_session_cls.return_value = build_mock_session(exception=dns_failure)
        resp = client.post(
            "/api/analyze",
            json={"url": "https://this-domain-does-not-exist.invalid"},
        )

    # The endpoint must never raise — it always returns 200 with a
    # success=False payload describing what went wrong.
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "Network error" in data["error"]
    assert data["title"] is None
    assert data["word_count"] == 0


def test_empty_url_is_rejected_without_making_a_request():
    with patch("main.AsyncSession") as mock_session_cls:
        resp = client.post("/api/analyze", json={"url": "   "})

    # No network call should have been attempted at all.
    mock_session_cls.assert_not_called()

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "URL cannot be empty."


# ---------------------------------------------------------------------------
# 3. Failure case: non-HTML response / timeout
# ---------------------------------------------------------------------------

def test_non_html_response_is_rejected():
    fake_response = FakeResponse(
        status_code=200,
        headers={"content-type": "application/pdf"},
        text="%PDF-1.4 ...binary...",
        url="https://example.com/file.pdf",
    )

    with patch("main.AsyncSession") as mock_session_cls:
        mock_session_cls.return_value = build_mock_session(response=fake_response)
        resp = client.post("/api/analyze", json={"url": "https://example.com/file.pdf"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "URL must be an HTML page."


def test_image_response_is_rejected():
    fake_response = FakeResponse(
        status_code=200,
        headers={"content-type": "image/png"},
        text="",
        url="https://example.com/logo.png",
    )

    with patch("main.AsyncSession") as mock_session_cls:
        mock_session_cls.return_value = build_mock_session(response=fake_response)
        resp = client.post("/api/analyze", json={"url": "https://example.com/logo.png"})

    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "URL must be an HTML page."


def test_timeout_returns_server_timeout_error():
    with patch("main.AsyncSession") as mock_session_cls:
        mock_session_cls.return_value = build_mock_session(
            exception=CurlTimeout("Request timed out")
        )
        resp = client.post("/api/analyze", json={"url": "https://slow.example"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "Server timeout."


# ---------------------------------------------------------------------------
# Sanity check on the mocking approach itself
# ---------------------------------------------------------------------------

def test_no_real_network_call_is_made_in_happy_path():
    """Guards against someone accidentally removing the patch and the suite
    silently starting to hit the real internet."""
    fake_response = FakeResponse(
        status_code=200,
        headers={"content-type": "text/html"},
        text=SAMPLE_HTML,
        url="https://example.com/",
    )

    with patch("main.AsyncSession") as mock_session_cls:
        session_mock = build_mock_session(response=fake_response)
        mock_session_cls.return_value = session_mock
        client.post("/api/analyze", json={"url": "https://example.com"})

        mock_session_cls.assert_called_once()
        session_mock.get.assert_awaited_once_with("https://example.com")
