# Page Pulse

Page Pulse is a lightweight technical website auditing tool. Give it a URL, and it fetches the page, parses its HTML, and reports back the metrics that matter for SEO and accessibility hygiene — in a few hundred milliseconds, with no sign-up and no heavyweight tooling.

**Live app:** https://page-pulse-7bu6.onrender.com

## Overview & Features

A FastAPI backend fetches the target page with a browser-impersonating HTTP client, parses it with BeautifulSoup, and returns a structured JSON report. The same FastAPI service also serves the frontend directly, so the whole app — UI and API — lives at a single URL.

Page Pulse audits:

- **Document title** — the `<title>` tag content
- **Meta description** — presence and content of `<meta name="description">`
- **H1 count** — number of `<h1>` headings on the page (flags missing or duplicate H1s)
- **Images missing `alt` text** — a quick accessibility signal
- **Approximate word count** — visible text volume, script/style/nav chrome excluded
- **HTTP status code & response time** — how the page actually responded, and how fast
- **PDF export** — one click turns the on-screen report into a downloadable PDF, generated entirely in the browser

## Tech Stack

| Layer     | Choice                                             |
| --------- | --------------------------------------------------- |
| Backend   | FastAPI, `curl_cffi` (browser-impersonating async HTTP client), BeautifulSoup4 |
| Frontend  | Vanilla HTML / CSS / JavaScript, `html2pdf.js` for PDF export, served directly by FastAPI |
| Hosting   | Render (single web service) |
| Testing   | `pytest`, FastAPI `TestClient`, `unittest.mock`    |

## Local Setup Instructions

### 1. Clone the repo

```bash
git clone https://github.com/Ch-anvitha/page-pulse.git
cd page-pulse/backend
```

Everything — `main.py`, `index.html`, and requirements — lives inside `backend/`, since FastAPI serves the frontend itself.

### 2. Set up a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate
# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the server

```bash
uvicorn main:app --reload --port 8000
```

That's it — open **`http://127.0.0.1:8000`** in your browser and you'll see the full Page Pulse UI. There's no separate frontend step: FastAPI serves `index.html` at `/` and the API at `/api/analyze` from the same process, so it behaves exactly like production. Interactive API docs are available at `http://127.0.0.1:8000/docs`.

### 5. Run the test suite (optional but recommended)

```bash
pip install -r requirements-dev.txt
pytest test_main.py -v
```

All tests run fully offline against a mocked HTTP client — no real network calls, no flaky results from sites being slow or blocking the request.

## API Contract

### `POST /api/analyze`

(A duplicate route also exists at `POST /analyze` for routing resilience — both do the same thing.)

#### Request body

```json
{
  "url": "https://example.com"
}
```

| Field | Type   | Required | Notes                                                                 |
| ----- | ------ | -------- | ---------------------------------------------------------------------- |
| `url` | string | yes      | With or without a scheme — `example.com` is normalized to `https://example.com` automatically |

#### Success response — `200 OK`

```json
{
  "success": true,
  "url": "https://example.com/",
  "status_code": 200,
  "response_time_ms": 214,
  "title": "Example Domain",
  "meta_description": "This domain is for use in illustrative examples.",
  "h1_count": 1,
  "images_missing_alt": 2,
  "word_count": 187,
  "error": null
}
```

#### Failure response — `200 OK` (error described in the body, not via HTTP status)

Page Pulse always returns HTTP 200 from this endpoint. Failures are communicated through `success: false` and a human-readable `error` message, so the frontend never has to distinguish "the API is down" from "the audit couldn't complete" — both come back as normal, parseable JSON.

```json
{
  "success": false,
  "url": "https://this-domain-does-not-exist.invalid",
  "status_code": null,
  "response_time_ms": null,
  "title": null,
  "meta_description": null,
  "h1_count": 0,
  "images_missing_alt": 0,
  "word_count": 0,
  "error": "Network error: Could not resolve host: this-domain-does-not-exist.invalid"
}
```

Errors you may see in `error`:

| Scenario                          | Message                             |
| ---------------------------------- | ------------------------------------ |
| Empty URL submitted                | `"URL cannot be empty."`            |
| DNS failure, connection refused, etc. | `"Network error: <details>"`     |
| Request exceeded the timeout        | `"Server timeout."`                |
| Response isn't an HTML page (PDF, image, etc.) | `"URL must be an HTML page."` |
| HTML fetched but parsing failed     | `"Parsing error: <details>"`       |

## Key Architectural Design Decisions

### 1. Client-side PDF generation with `html2pdf.js`, not server-side rendering

The report is exported to PDF entirely in the browser using `html2pdf.js`, rather than generating it on the FastAPI backend with something like WeasyPrint or a headless-Chromium screenshot service.

**Why:** server-side PDF rendering — especially anything backed by headless Chromium — is a heavy dependency to add to the backend. It increases memory usage and startup time, and introduces a whole extra failure surface (missing fonts, sandboxing issues, memory limits) for a feature that's purely cosmetic. Since the report the user wants exported is already rendered as HTML/CSS in the DOM, `html2pdf.js` can turn exactly what's on screen into a PDF client-side, instantly, with zero backend involvement, zero added server load, and zero risk of it breaking the audit endpoint itself.

### 2. Graceful timeout and exception handling around every fetch

Every outbound request in `/api/analyze` is wrapped in a `try`/`except` block with an explicit 8-second timeout, and **every** exception — DNS failures, connection resets, malformed responses, parsing errors — is caught and converted into a structured `AuditResponse` with `success: false`, rather than being allowed to propagate.

**Why:** the whole point of an *auditing* tool is that it's pointed at arbitrary, unpredictable third-party URLs, so the failure modes are inherently unbounded — malformed redirects, refused connections, slow or unresponsive servers, corrupted responses, TLS failures. Without a timeout, a single unresponsive site could hang the request indefinitely and tie up a worker. This decision was originally shaped by an earlier deployment on Vercel, where serverless functions have a hard 10-second execution ceiling — the 8-second timeout was set deliberately under that limit so the function always had time to catch the failure and respond cleanly before the platform itself killed it. The app now runs on Render as a persistent server rather than a serverless function, but the underlying reasoning still holds: catching broadly and always returning a structured response (never letting an exception propagate, never leaving a request hanging) keeps the contract with the frontend simple — this endpoint never crashes and never hangs, it always returns *something* the UI can render.

### 3. Vanilla JavaScript with CSS-driven 3D cards, not a frontend framework

The frontend is a single static `index.html` file with plain JavaScript and CSS (including the 3D tilt effects on the metric cards), rather than a React or Vue application.

**Why:** the frontend's job here is genuinely simple — take a URL, POST it, render the JSON response into some cards. That doesn't need component state management, a virtual DOM, or client-side routing. Reaching for a framework would mean a build step (bundler config, `node_modules`, a build pipeline to keep in sync with the backend deploy), for a UI that's a handful of DOM updates. Going vanilla means the entire frontend is one file with no build step at all — FastAPI can serve it directly via a single `FileResponse`, with no separate static-hosting service, no build artifacts to keep in sync, and no framework runtime to download on page load. The 3D perspective effects are pure CSS `transform`/`perspective` driven by a few lines of mouse-tracking JS — a nice-to-have visual layer that doesn't justify pulling in tooling built for managing complex, stateful UIs.

## Project Structure

```
page-pulse/
└── backend/
    ├── main.py                 # FastAPI app: serves index.html at "/", audits at /api/analyze
    ├── index.html              # frontend (single file, served directly by FastAPI)
    ├── test_main.py            # pytest suite (mocked, offline)
    ├── requirements.txt        # production dependencies
    └── requirements-dev.txt    # test-only dependencies
```

## Deployment

Page Pulse is deployed on **Render** as a single web service:

- **Root Directory:** `backend`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`

Because `main.py` serves `index.html` directly via a `FileResponse` at `GET /`, no separate static-site service is needed — one Render web service handles both the UI and the API, at one URL. Pushing to the connected Git branch triggers an automatic rebuild and redeploy.
## Deployment

Page Pulse is deployed on Vercel. The FastAPI app in `backend/main.py` is picked up automatically as a Python serverless function, and `index.html` is served as a static asset from the same domain — which is what lets the frontend call `/api/analyze` with a relative path.

Pushing to the connected Git branch triggers an automatic rebuild and redeploy.
