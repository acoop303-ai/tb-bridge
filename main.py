"""
ThriftBooks Buyback Price Middleware
====================================
FastAPI server that uses curl_cffi (Chrome TLS impersonation) + residential proxy
to fetch TB buyback prices server-side — bypassing Cloudflare/DataDome.

Deploy on Railway: railway.app (free tier works)

Environment variables:
  PROXY_URI    - Residential proxy URL, e.g. http://user:pass@gate.iproyal.com:7777
  TB_COOKIES   - Optional: your TB session cookies (paste from browser DevTools)
                 Format: "TIdent=xxx; tbs=yyy; CartIdentifier=zzz"
  API_KEY      - Optional: secret key to protect your endpoint

Endpoints:
  GET /health              - health check
  GET /tb?isbn=X           - single ISBN lookup
  GET /tb/batch?isbns=X,Y  - batch lookup (comma-separated ISBNs)
"""

import os
import re
import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as cffi_requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tb-bridge")

app = FastAPI(title="TB Price Bridge", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Config from env ────────────────────────────────────────────────────────────

PROXY_URI     = os.getenv("PROXY_URI", "")
TB_COOKIES    = os.getenv("TB_COOKIES", "")
API_KEY       = os.getenv("API_KEY", "")   # optional — leave blank to disable auth

PROXIES = {"https": PROXY_URI, "http": PROXY_URI} if PROXY_URI else {}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# ── Session (persistent across requests — keeps cookies alive) ─────────────────

def build_session() -> cffi_requests.Session:
    s = cffi_requests.Session(impersonate="chrome124")
    if TB_COOKIES:
        for part in TB_COOKIES.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=".thriftbooks.com")
    return s

_session: Optional[cffi_requests.Session] = None

def get_session() -> cffi_requests.Session:
    global _session
    if _session is None:
        _session = build_session()
    return _session

# ── Auth check ─────────────────────────────────────────────────────────────────

def check_auth(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ── Helpers ────────────────────────────────────────────────────────────────────

def clean_isbn(raw: str) -> str:
    return re.sub(r"[^\d]", "", str(raw))

def empty_result(isbn: str, error: str = "") -> dict:
    return {"price": 0.0, "wants": 0, "accept": False, "error": error}

# ── Core fetch logic ───────────────────────────────────────────────────────────

def fetch_prices(isbns: list[str]) -> dict:
    """
    Attempt 1: TB JSON API  (/tb-api/buyback/get-quotes/)
    Attempt 2: HTML scrape  (/buyback/?isbn=X)  — per book, slower
    """
    s = get_session()
    results = {}

    try:
        # ── Warmup: load buyback page so Cloudflare sees normal browsing ──────
        log.info(f"Warming up session for {len(isbns)} ISBNs")
        warmup = s.get(
            "https://www.thriftbooks.com/buyback/",
            headers={**HEADERS, "Referer": "https://www.thriftbooks.com/"},
            proxies=PROXIES or None,
            timeout=15,
        )
        log.info(f"Warmup status: {warmup.status_code}")

        # ── Get CSRF token ────────────────────────────────────────────────────
        csrf_resp = s.get(
            "https://www.thriftbooks.com/tb-api/csrf/GetToken",
            headers={**HEADERS, "Accept": "application/json", "Referer": "https://www.thriftbooks.com/buyback/"},
            proxies=PROXIES or None,
            timeout=10,
        )
        log.info(f"CSRF status: {csrf_resp.status_code}")

        csrf_token = ""
        if csrf_resp.status_code == 200:
            try:
                csrf_token = csrf_resp.json().get("token", "")
            except Exception:
                pass

        if csrf_token:
            # ── API path: batch quotes ────────────────────────────────────────
            log.info(f"Got CSRF token, hitting API for {len(isbns)} ISBNs")
            results = _api_fetch(s, isbns, csrf_token)
        else:
            # ── Fallback: HTML scrape per book ────────────────────────────────
            log.warning("No CSRF token — falling back to HTML scrape")
            results = _html_fetch(s, isbns)

    except Exception as e:
        log.error(f"Session error: {e}")
        for isbn in isbns:
            results[isbn] = empty_result(isbn, str(e))

    # Fill any missing
    for isbn in isbns:
        if isbn not in results:
            results[isbn] = empty_result(isbn, "not_found")

    return results


def _api_fetch(s, isbns: list[str], csrf_token: str) -> dict:
    results = {}
    for i in range(0, len(isbns), 20):
        batch = isbns[i:i + 20]
        payload = {
            "identifiers": [{"identifier": isbn, "identifierType": "isbn"} for isbn in batch],
            "addedFrom": 3,
        }
        try:
            resp = s.post(
                "https://www.thriftbooks.com/tb-api/buyback/get-quotes/",
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-XSRF-TOKEN": csrf_token,
                    "Referer": "https://www.thriftbooks.com/buyback/",
                },
                json=payload,
                proxies=PROXIES or None,
                timeout=20,
            )
            log.info(f"API quote status: {resp.status_code}, body[:80]: {resp.text[:80]}")

            if resp.status_code == 200:
                data = resp.json()
                for item in (data.get("sellListItems") or []):
                    isbn = item.get("userEnteredIdentifier", "")
                    price = float(item.get("quotePrice") or item.get("sellPrice") or 0)
                    wants = int(item.get("desiredQuantity") or item.get("wantCount") or 0)
                    accept = bool(item.get("isAccepted")) or price > 0
                    results[isbn] = {"price": price, "wants": wants, "accept": accept, "error": ""}
            else:
                # Blocked — fall back to HTML for this batch
                html_batch = _html_fetch(s, batch)
                results.update(html_batch)

        except Exception as e:
            log.error(f"API batch error: {e}")
            html_batch = _html_fetch(s, batch)
            results.update(html_batch)

    return results


def _html_fetch(s, isbns: list[str]) -> dict:
    """Scrape the buyback page HTML per ISBN — slower but works without API auth."""
    results = {}
    for isbn in isbns:
        try:
            resp = s.get(
                f"https://www.thriftbooks.com/buyback/?isbn={isbn}",
                headers={**HEADERS, "Referer": "https://www.thriftbooks.com/buyback/"},
                proxies=PROXIES or None,
                timeout=15,
            )
            html = resp.text

            # Explicit rejection messages
            if "not accepting this title" in html or "adequate/excess stock" in html:
                results[isbn] = {"price": 0.0, "wants": 0, "accept": False, "error": "not_buying"}
                continue

            # Try to parse price from HTML
            m = re.search(r'buyback-price["\'][^>]*>\s*\$(\d+\.\d{2})', html)
            if m:
                price = float(m.group(1))
                results[isbn] = {"price": price, "wants": 0, "accept": True, "error": ""}
            else:
                results[isbn] = {"price": 0.0, "wants": 0, "accept": False, "error": "no_price_in_html"}

        except Exception as e:
            results[isbn] = empty_result(isbn, str(e))

    return results


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "proxy": bool(PROXY_URI), "cookies": bool(TB_COOKIES)}


@app.get("/tb")
def get_tb_price(isbn: str, x_api_key: Optional[str] = Header(default=None)):
    """Single ISBN lookup. Returns {isbn, price, wants, accept, error}"""
    check_auth(x_api_key)
    isbn = clean_isbn(isbn)
    if len(isbn) not in (10, 13):
        raise HTTPException(400, "Invalid ISBN — must be 10 or 13 digits")

    result = fetch_prices([isbn])
    data = result.get(isbn, empty_result(isbn))
    return {"isbn": isbn, **data}


@app.get("/tb/batch")
def get_tb_prices_batch(isbns: str, x_api_key: Optional[str] = Header(default=None)):
    """
    Batch lookup. ?isbns=ISBN1,ISBN2,ISBN3
    Returns {items: {isbn: {price, wants, accept, error}}}
    """
    check_auth(x_api_key)
    isbn_list = [clean_isbn(i) for i in isbns.split(",")]
    isbn_list = [i for i in isbn_list if len(i) in (10, 13)]
    if not isbn_list:
        raise HTTPException(400, "No valid ISBNs provided")

    results = fetch_prices(isbn_list)
    return {"items": results, "count": len(results)}
