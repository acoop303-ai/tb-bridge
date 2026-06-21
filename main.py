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
import time
import logging
import urllib.parse
import statistics
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup

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
TB_COOKIES    = os.getenv("TB_COOKIES", "")   # fallback — can also be passed per-request
API_KEY       = os.getenv("API_KEY", "")      # optional — leave blank to disable auth

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

def build_session(cookie_str: str = "") -> cffi_requests.Session:
    s = cffi_requests.Session(impersonate="chrome124")
    cookies = cookie_str or TB_COOKIES
    if cookies:
        for part in cookies.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=".thriftbooks.com")
    return s

_session: Optional[cffi_requests.Session] = None

def get_session(cookie_str: str = "") -> cffi_requests.Session:
    global _session
    # If caller passes cookies, always build a fresh session with them
    if cookie_str:
        return build_session(cookie_str)
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

def fetch_prices(isbns: list[str], cookie_str: str = "") -> dict:
    """
    Attempt 1: TB JSON API  (/tb-api/buyback/get-quotes/)
    Attempt 2: HTML scrape  (/buyback/?isbn=X)  — per book, slower
    """
    s = get_session(cookie_str)
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
def get_tb_price(
    isbn: str,
    x_api_key: Optional[str] = Header(default=None),
    x_tb_cookies: Optional[str] = Header(default=None),
):
    """Single ISBN lookup. Returns {isbn, price, wants, accept, error}"""
    check_auth(x_api_key)
    isbn = clean_isbn(isbn)
    if len(isbn) not in (10, 13):
        raise HTTPException(400, "Invalid ISBN — must be 10 or 13 digits")

    result = fetch_prices([isbn], cookie_str=x_tb_cookies or "")
    data = result.get(isbn, empty_result(isbn))
    return {"isbn": isbn, **data}


@app.get("/tb/batch")
def get_tb_prices_batch(
    isbns: str,
    x_api_key: Optional[str] = Header(default=None),
    x_tb_cookies: Optional[str] = Header(default=None),
):
    """
    Batch lookup. ?isbns=ISBN1,ISBN2,ISBN3
    Returns {items: {isbn: {price, wants, accept, error}}}
    """
    check_auth(x_api_key)
    isbn_list = [clean_isbn(i) for i in isbns.split(",")]
    isbn_list = [i for i in isbn_list if len(i) in (10, 13)]
    if not isbn_list:
        raise HTTPException(400, "No valid ISBNs provided")

    results = fetch_prices(isbn_list, cookie_str=x_tb_cookies or "")
    return {"items": results, "count": len(results)}


# ── eBay Sold Prices ───────────────────────────────────────────────────────────

EBAY_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "upgrade-insecure-requests": "1",
}
_ebay_session = None

def get_ebay_session():
    global _ebay_session
    if _ebay_session is None:
        s = cffi_requests.Session(impersonate="chrome124")
        s.get("https://www.ebay.com/", headers={**EBAY_HEADERS, "sec-fetch-site": "none"}, timeout=15)
        _ebay_session = s
        log.info("eBay session warmed up")
    return _ebay_session

def scrape_ebay_sold(query: str, days: int, seen: set) -> list:
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=days)
    listings = []
    base = ("https://www.ebay.com/sch/i.html"
            f"?_nkw={urllib.parse.quote(query)}&LH_Sold=1&LH_Complete=1&_sop=13&_ipg=60")
    s = get_ebay_session()
    for pg in range(1, 6):
        try:
            resp = s.get(base + f"&_pgn={pg}",
                         headers={**EBAY_HEADERS, "sec-fetch-site": "same-origin",
                                  "Referer": "https://www.ebay.com/"}, timeout=20)
            if resp.status_code != 200:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select("ul.srp-results li.s-card")
            if not cards:
                break
            hit_cutoff = False
            new_ct = 0
            for card in cards:
                pel = card.select_one("span.s-card__price")
                if not pel: continue
                m = re.search(r"[\d,]+\.?\d*", pel.get_text().replace(",",""))
                if not m: continue
                price = float(m.group())
                if not (0.50 <= price <= 500): continue
                tel = card.select_one("span.su-styled-text.primary")
                title = tel.get_text(strip=True)[:80] if tel else ""
                lel = card.select_one("a.s-card__link")
                url = lel["href"].split("?")[0] if lel and lel.get("href") else ""
                sold_date = None; date_str = "unknown"
                for de in card.select("span.su-styled-text.positive"):
                    if "s-card__price" in (de.get("class") or []): continue
                    dt = re.sub(r"^Sold\s+","",de.get_text(strip=True),flags=re.IGNORECASE).strip()
                    for fmt_str in ("%b %d, %Y","%d %b %Y","%B %d, %Y"):
                        try: sold_date=datetime.strptime(dt,fmt_str); date_str=sold_date.strftime("%Y-%m-%d"); break
                        except ValueError: pass
                    if sold_date: break
                if sold_date and sold_date < cutoff: hit_cutoff=True; break
                sec = card.select("span.su-styled-text.secondary")
                condition = sec[0].get_text(strip=True) if sec else ""
                key = url or f"{price}_{date_str}_{title[:15]}"
                if key in seen: continue
                seen.add(key)
                listings.append({"price":price,"title":title,"date":date_str,"condition":condition,"url":url})
                new_ct += 1
            if hit_cutoff or new_ct == 0: break
            time.sleep(0.4)
        except Exception as e:
            log.warning(f"eBay scrape error pg{pg}: {e}")
            break
    return listings

NEW_KW = ("brand new","new with","factory sealed")

def flag_outliers(listings):
    if len(listings) < 4: return listings
    prices = sorted(l["price"] for l in listings)
    q1 = statistics.quantiles(prices, n=4)[0]
    q3 = statistics.quantiles(prices, n=4)[2]
    iqr = q3 - q1
    lo, hi = q1 - 1.5*iqr, q3 + 1.5*iqr
    for l in listings:
        l["outlier"] = l["price"] < lo or l["price"] > hi
    return listings

def ebay_stats(listings, exclude_outliers=False):
    if not listings: return {}
    src = [l for l in listings if not l.get("outlier")] if exclude_outliers else listings
    if not src: src = listings
    prices = sorted(l["price"] for l in src)
    return {
        "count": len(listings),
        "count_clean": len([l for l in listings if not l.get("outlier")]),
        "outlier_count": len([l for l in listings if l.get("outlier")]),
        "low": prices[0], "high": prices[-1],
        "median": round(statistics.median(prices), 2),
        "avg": round(statistics.mean(prices), 2),
    }

@app.get("/ebay")
def get_ebay_sold(
    isbn: str,
    days: int = 365,
    x_api_key: Optional[str] = Header(default=None),
):
    """
    GET /ebay?isbn=X&days=365
    Returns used sold median/avg + recent listings for an ISBN.
    """
    check_auth(x_api_key)
    isbn = clean_isbn(isbn)
    if len(isbn) not in (10, 13):
        raise HTTPException(400, "Invalid ISBN")

    seen: set = set()
    try:
        isbn_results = scrape_ebay_sold(isbn, days, seen)
        # Title search via OpenLibrary
        title = ""
        try:
            import urllib.request
            url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
            with urllib.request.urlopen(url, timeout=6) as r:
                data = json.loads(r.read())
            title = data.get(f"ISBN:{isbn}", {}).get("title", "")
        except Exception:
            pass
        title_results = scrape_ebay_sold(title, days, seen) if title else []
        all_listings = isbn_results + title_results
    except Exception as e:
        log.error(f"eBay fetch error: {e}")
        global _ebay_session; _ebay_session = None  # reset session on error
        return {"isbn": isbn, "error": str(e), "used": {}, "listings": []}

    NEW_KW = ("brand new", "new with", "factory sealed")
    used = [l for l in all_listings if not any(k in l.get("condition","").lower() for k in NEW_KW)]
    new  = [l for l in all_listings if     any(k in l.get("condition","").lower() for k in NEW_KW)]
    used = flag_outliers(used)

    all_listings.sort(key=lambda x: x.get("date",""), reverse=True)

    return {
        "isbn": isbn,
        "title": title,
        "days": days,
        "used": ebay_stats(used, exclude_outliers=True),
        "new":  ebay_stats(new),
        "all":  ebay_stats(all_listings),
        "listings": all_listings,
        "error": "",
    }
