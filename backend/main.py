import time
import re
from typing import Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
from bs4 import BeautifulSoup

app = FastAPI(title="Page Pulse API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AuditRequest(BaseModel):
    url: str

class AuditResponse(BaseModel):
    success: bool
    url: str
    status_code: Optional[int] = None
    response_time_ms: Optional[int] = None
    title: Optional[str] = None
    meta_description: Optional[str] = None
    h1_count: Optional[int] = 0
    images_missing_alt: Optional[int] = 0
    word_count: Optional[int] = 0
    error: Optional[str] = None

def normalize_url(raw_url: str) -> str:
    cleaned = raw_url.strip()
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    return cleaned

# Serves the frontend directly from this same service, so the whole app
# (UI + API) lives at one Render URL instead of needing a separate static site.
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

@app.post("/api/analyze")
@app.post("/analyze")
async def analyze_url(payload: AuditRequest):
    raw_url = payload.url.strip()
    
    if not raw_url:
        return AuditResponse(success=False, url=raw_url, error="URL cannot be empty.")

    target_url = normalize_url(raw_url)
    start_time = time.perf_counter()

    try:
        async with AsyncSession(timeout=8.0, allow_redirects=True, impersonate="chrome") as session:
            response = await session.get(target_url)

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

    except CurlTimeout:
        return AuditResponse(success=False, url=target_url, error="Server timeout.")
    except Exception as exc:
        return AuditResponse(success=False, url=target_url, error=f"Network error: {str(exc)}")

    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return AuditResponse(success=False, url=target_url, error="URL must be an HTML page.")

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        meta_description = meta_desc.get("content", "").strip() if meta_desc else None

        h1_count = len(soup.find_all("h1"))
        
        images_missing_alt = sum(1 for img in soup.find_all("img") if not img.get("alt", "").strip())

        for el in soup(["script", "style", "noscript", "header", "footer", "svg"]):
            el.decompose()
        
        words = re.findall(r'\b\w+\b', soup.get_text(separator=" "))
        
        return AuditResponse(
            success=True,
            url=str(response.url),
            status_code=response.status_code,
            response_time_ms=elapsed_ms,
            title=title,
            meta_description=meta_description,
            h1_count=h1_count,
            images_missing_alt=images_missing_alt,
            word_count=len(words)
        )

    except Exception as exc:
        return AuditResponse(success=False, url=target_url, error=f"Parsing error: {str(exc)}")