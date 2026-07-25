"""
Page Pulse — Backend API
================================================================================
FastAPI service that audits a URL for performance and content-quality metrics.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession, RequestsError
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

# ------------------------------------------------------------------------------
# Logging & Setup
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("page_pulse")

# Initialize FastAPI App (Only Once)
app = FastAPI(
    title="Page Pulse API",
    description="Fetches a URL and returns performance + content-quality metrics.",
    version="2.0.0",
)

# Configure CORS Middleware (Only Once)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with specific frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# Network / App configuration
# ------------------------------------------------------------------------------
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 5
IMPERSONATE_PROFILE = "chrome124"

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

NON_VISIBLE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "header",
    "footer",
    "nav",
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# ------------------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    """Incoming request body for a page audit."""
    url: str = Field(..., description="Target URL to analyze.", examples=["https://example.com"])

    @field_validator("url")
    @classmethod
    def validate_and_normalize_url(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("URL cannot be empty.")

        if not value.lower().startswith(("http://", "https://")):
            value = f"https://{value}"

        scheme, _, rest = value.partition("://")
        host = rest.split("/", 1)[0]
        if not host or "." not in host:
            raise ValueError("URL must include a valid domain, e.g. https://example.com")

        return value


class AnalyzeResponse(BaseModel):
    """Unified response shape for both successful and failed audits."""
    url: str
    success: bool
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    title: Optional[str] = None
    meta_description: Optional[str] = None
    h1_count: Optional[int] = None
    images_missing_alt: Optional[int] = None
    word_count: Optional[int] = None
    error: Optional[str] = None
    error_type: Optional[str] = None

# ------------------------------------------------------------------------------
# Exception Handlers
# ------------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    raw_url = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_url = str(body.get("url", "") or "")
    except Exception:
        pass

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=AnalyzeResponse(
            url=raw_url,
            success=False,
            error="Please provide a valid URL, e.g. https://example.com",
            error_type="invalid_url",
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=AnalyzeResponse(
            url="",
            success=False,
            error="An unexpected server error occurred. Please try again.",
            error_type="unknown_error",
        ).model_dump(),
    )

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def _first_meta_content(soup: BeautifulSoup, *, name: Optional[str] = None, property_: Optional[str] = None) -> Optional[str]:
    tag = None
    if property_ is not None:
        tag = soup.find("meta", property=property_) or soup.find("meta", attrs={"name": property_})
    if tag is None and name is not None:
        tag = soup.find(
            "meta",
            attrs={"name": lambda v: isinstance(v, str) and v.strip().lower() == name},
        )
    if tag and tag.get("content"):
        content = tag["content"].strip()
        return content or None
    return None


def _extract_title(soup: BeautifulSoup) -> Optional[str]:
    if soup.title and soup.title.string:
        text = soup.title.get_text(strip=True)
        if text:
            return text

    for property_ in ("og:title", "twitter:title"):
        fallback = _first_meta_content(soup, property_=property_)
        if fallback:
            return fallback

    return None


def _extract_meta_description(soup: BeautifulSoup) -> Optional[str]:
    direct = _first_meta_content(soup, name="description")
    if direct:
        return direct

    for property_ in ("og:description", "twitter:description"):
        fallback = _first_meta_content(soup, property_=property_)
        if fallback:
            return fallback

    return None


def _count_images_missing_alt(soup: BeautifulSoup) -> int:
    images = soup.find_all("img")
    return sum(1 for img in images if not (img.get("alt") or "").strip())


def _count_visible_words(soup: BeautifulSoup) -> int:
    for tag in soup.find_all(NON_VISIBLE_TAGS):
        tag.decompose()

    visible_text = soup.get_text(separator=" ", strip=True)
    return len(visible_text.split()) if visible_text else 0


def extract_metrics(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = _extract_title(soup)
    meta_description = _extract_meta_description(soup)
    h1_count = len(soup.find_all("h1"))
    images_missing_alt = _count_images_missing_alt(soup)
    word_count = _count_visible_words(soup)

    return {
        "title": title,
        "meta_description": meta_description,
        "h1_count": h1_count,
        "images_missing_alt": images_missing_alt,
        "word_count": word_count,
    }


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)

# ------------------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------------------
@app.get("/")
def read_root():
    return {"message": "Page Pulse API is running!"}


@app.get("/api/health")
async def health_check() -> dict:
    return {"status": "ok"}


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_page(payload: AnalyzeRequest) -> AnalyzeResponse:
    target_url = payload.url
    start = time.perf_counter()

    try:
        async with AsyncSession(
            impersonate=IMPERSONATE_PROFILE,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers=BROWSER_HEADERS,
            allow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as session:
            response = await session.get(target_url)

        elapsed_ms = _elapsed_ms(start)

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            return AnalyzeResponse(
                url=target_url,
                success=False,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                error=f"URL did not return HTML content (got '{content_type or 'unknown'}').",
                error_type="non_html_content",
            )

        if response.status_code in (403, 429):
            return AnalyzeResponse(
                url=target_url,
                success=False,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                error=(
                    f"Site returned HTTP {response.status_code}, likely a bot/rate-limit "
                    "block despite browser impersonation."
                ),
                error_type="waf_block",
            )

        if response.status_code >= 400:
            return AnalyzeResponse(
                url=target_url,
                success=False,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                error=f"Page responded with HTTP error status {response.status_code}.",
                error_type="http_error",
            )

        try:
            metrics = extract_metrics(response.text)
        except Exception as parse_exc:
            logger.warning("Parsing failed for %s: %s", target_url, parse_exc)
            return AnalyzeResponse(
                url=target_url,
                success=False,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                error="Page was fetched but its HTML could not be parsed.",
                error_type="parse_error",
            )

        return AnalyzeResponse(
            url=target_url,
            success=True,
            status_code=response.status_code,
            response_time_ms=elapsed_ms,
            **metrics,
        )

    except CurlTimeout as exc:
        logger.info("Timeout fetching %s: %s", target_url, exc)
        return AnalyzeResponse(
            url=target_url,
            success=False,
            response_time_ms=_elapsed_ms(start),
            error=f"Request timed out after {REQUEST_TIMEOUT_SECONDS:.0f}s.",
            error_type="timeout",
        )

    except CurlConnectionError as exc:
        logger.info("Connection error for %s: %s", target_url, exc)
        return AnalyzeResponse(
            url=target_url,
            success=False,
            response_time_ms=_elapsed_ms(start),
            error="Could not connect to the host (DNS failure, refused connection, or unreachable server).",
            error_type="connection_error",
        )

    except RequestsError as exc:
        logger.info("Request error for %s: %s", target_url, exc)
        return AnalyzeResponse(
            url=target_url,
            success=False,
            response_time_ms=_elapsed_ms(start),
            error=f"Network error while fetching the page: {exc}",
            error_type="request_error",
        )

    except Exception:
        logger.exception("Unexpected error analyzing %s", target_url)
        return AnalyzeResponse(
            url=target_url,
            success=False,
            response_time_ms=_elapsed_ms(start),
            error="An unexpected error occurred while analyzing the page.",
            error_type="unknown_error",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)