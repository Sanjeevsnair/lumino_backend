#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lumino API — LookMovie2 Provider
FastAPI backend with /api/search, /api/get-links, /api/extract
"""

import asyncio
import json
import logging
import os
import re
import time
import threading
import urllib.parse
import concurrent.futures
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# BeautifulSoup replaced by Playwright for more robust scraping
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

# =====================================================================
# Logging
# =====================================================================

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("lumino")

# =====================================================================
# Constants
# =====================================================================

BASE_URL = "https://www.lookmovie2.to"

# =====================================================================
# Environment-based bypass config
# =====================================================================

FLARE_URL  = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")
HTTP_PROXY = os.environ.get("HTTP_PROXY", "") or os.environ.get("HTTPS_PROXY", "")
RELAY_URL  = os.environ.get("RELAY_URL", "").rstrip("/")

_PROXIES = {"http": HTTP_PROXY, "https": HTTP_PROXY} if HTTP_PROXY else {}

# =====================================================================
# HTTP Layer
# =====================================================================

_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

def _relay_get(url: str, timeout: int = 25, headers: dict = None):
    if not RELAY_URL: return None
    try:
        import httpx
        relay_url = f"{RELAY_URL}/?url={urllib.parse.quote(url, safe='')}"
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
            return client.get(relay_url, headers={**_BASE_HEADERS, **(headers or {})})
    except Exception: return None

def _flaresolverr_get(url: str, timeout: int = 60, headers: dict = None):
    if not FLARE_URL: return None
    try:
        import requests as _req
        payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}
        r = _req.post(f"{FLARE_URL}/v1", json=payload, timeout=timeout + 10, proxies=_PROXIES or None)
        data = r.json()
        if data.get("status") == "ok":
            sol = data["solution"]
            class _FakeResp:
                status_code = sol.get("status", 200)
                text = sol.get("response", "")
            return _FakeResp()
    except Exception: return None

_cffi_session = None
_cffi_lock = threading.Lock()

def _get_cffi_session():
    global _cffi_session
    if _cffi_session is None:
        try:
            from curl_cffi import requests as cffi_requests
            _cffi_session = cffi_requests.Session(impersonate="chrome124")
        except Exception: pass
    return _cffi_session

def _cffi_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        with _cffi_lock:
            session = _get_cffi_session()
            if session is None: return None
            kwargs = dict(timeout=timeout, allow_redirects=True, headers={**_BASE_HEADERS, **(headers or {})})
            if HTTP_PROXY: kwargs["proxies"] = _PROXIES
            return session.get(url, **kwargs)
    except Exception: return None

def _requests_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        import requests
        h = {**_BASE_HEADERS, **(headers or {})}
        return requests.get(url, timeout=timeout, allow_redirects=True, headers=h, proxies=_PROXIES or None)
    except Exception: return None

def _cloud_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        import cloudscraper
        sc = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
        return sc.get(url, timeout=timeout, allow_redirects=True, headers=headers, proxies=_PROXIES or None)
    except Exception: return None

def _httpx_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        import httpx
        h = {**_BASE_HEADERS, **(headers or {})}
        with httpx.Client(http2=True, headers=h, timeout=timeout, follow_redirects=True, proxy=HTTP_PROXY or None) as client:
            return client.get(url)
    except Exception: return None

def _playwright_get(url: str, timeout: int = 30, headers: dict = None):
    """
    Final fallback: Use a real headless browser (Playwright).
    This is slow but very effective against Cloudflare.
    Requires: playwright install --with-deps chromium
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # We use chromium as it is the most standard for bypasses
            browser = p.chromium.launch(headless=True)
            # Use the same User-Agent as our other strategies
            context = browser.new_context(
                user_agent=_BASE_HEADERS["User-Agent"],
                viewport={'width': 1280, 'height': 720}
            )
            page = context.new_page()
            
            # Set headers if provided
            if headers:
                page.set_extra_http_headers(headers)
            
            # Navigate and wait for the page to be ready
            response = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            
            # Optional: handle short Cloudflare delays
            if "Cloudflare" in page.title() or "Just a moment" in page.content():
                page.wait_for_timeout(5000)
                
            content = page.content()
            status = response.status if response else 500
            browser.close()
            
            class _FakeResp:
                status_code = status
                text = content
            return _FakeResp()
    except Exception as e:
        logger.debug(f"[http:playwright] {url!r} → {e}")
        return None

def fetch_text(url: str, timeout: int = 25, headers: dict = None) -> Optional[str]:
    strategies = [
        ("relay", _relay_get if RELAY_URL else None),
        ("flaresolverr", _flaresolverr_get if FLARE_URL else None),
        ("cffi", _cffi_get),
        ("cloudscraper", _cloud_get),
        ("httpx", _httpx_get),
        ("playwright", _playwright_get),
        ("requests", _requests_get),
    ]
    for name, fn in strategies:
        if fn is None: continue
        try:
            resp = fn(url, timeout, headers)
            if resp and resp.status_code == 200:
                logger.debug(f"[fetch_text] {name} succeeded for {url!r}")
                return resp.text
        except Exception: pass
    return None

def fetch_json(url: str, timeout: int = 20, headers: dict = None) -> Optional[Any]:
    hdrs = {
        "Accept": "application/json, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_URL + "/",
    }
    if headers: hdrs.update(headers)
    text = fetch_text(url, timeout=timeout, headers=hdrs)
    if text:
        try: return json.loads(text)
        except Exception: pass
    return None

# =====================================================================
# LookMovie2 — Logic
# =====================================================================

def lm_search_movies(query: str, max_results: int = 10) -> List[Dict]:
    q = urllib.parse.quote_plus(query.strip())
    data = fetch_json(f"{BASE_URL}/api/v1/movies/do-search/?q={q}")
    if not data or "result" not in data: return []
    return [{
        "type": "Movie", "id": item.get("id_movie"), "slug": item.get("slug", ""),
        "title": item.get("title", "?"), "year": str(item.get("year", "N/A")),
        "rating": str(item.get("imdb_rating", "N/A")), "quality": item.get("quality_badge", "N/A"),
        "url": f"{BASE_URL}/movies/view/{item.get('slug', '')}", "source": "lookmovie"
    } for item in data["result"][:max_results]]

def lm_search_shows(query: str, max_results: int = 10) -> List[Dict]:
    q = urllib.parse.quote_plus(query.strip())
    data = fetch_json(f"{BASE_URL}/api/v1/shows/do-search/?q={q}")
    if not data or "result" not in data: return []
    return [{
        "type": "Show", "id": item.get("id_show"), "slug": item.get("slug", ""),
        "title": item.get("title", "?"), "year": str(item.get("year", "N/A")),
        "rating": str(item.get("imdb_rating", "N/A")), "quality": item.get("quality_badge", "N/A"),
        "url": f"{BASE_URL}/shows/view/{item.get('slug', '')}", "source": "lookmovie"
    } for item in data["result"][:max_results]]

def lm_get_episode_list(id_show: int) -> Dict[str, Dict[str, str]]:
    data = fetch_json(f"{BASE_URL}/api/v2/download/episode/list?id={id_show}")
    if not data or "list" not in data: return {}
    return {str(s): {str(e): str(d.get("id_episode", "")) for e, d in eps.items()} for s, eps in data["list"].items()}

def lm_movie_play_url(slug: str) -> str:
    return f"{BASE_URL}/movies/play/{slug}"

def lm_show_play_url(slug: str, id_show: int, season: int, episode: int) -> Tuple[str, str]:
    episodes = lm_get_episode_list(id_show)
    s, e = str(season), str(episode)
    if s not in episodes or e not in episodes[s]: raise ValueError(f"S{season}E{episode} not found")
    ep_id = episodes[s][e]
    return f"{BASE_URL}/shows/play/{slug}#S{season}-E{episode}-{ep_id}", ep_id

def _extract_storage(html: str, storage_name: str) -> Dict:
    pat = rf"window(?:(?:\['{re.escape(storage_name)}'\])|(?:\.{re.escape(storage_name)}))\s*=\s*\{{([^{{}}]+(?:\{{[^{{}}]*\}}[^{{}}]*)*)\}}"
    m = re.search(pat, html, re.S)
    if not m:
        pat_start = rf"window(?:(?:\['{re.escape(storage_name)}'\])|(?:\.{re.escape(storage_name)}))\s*=\s*\{{"
        sm = re.search(pat_start, html)
        if not sm: return {}
        start, depth, end = sm.end() - 1, 0, -1
        for i in range(start, len(html)):
            if html[i] == '{': depth += 1
            elif html[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1: return {}
        block = html[start + 1:end]
    else: block = m.group(1)
    res = {}
    for km in re.finditer(r"(\w+)\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|([-\d]+))", block):
        k, v = km.group(1), km.group(2) or km.group(3)
        if v is None and km.group(4): v = int(km.group(4))
        res[k] = v
    return res

def _parse_subtitles(raw_list: list) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for entry in raw_list or []:
        lang = entry.get("language", "Unknown")
        raw_file = entry.get("file", "")
        paths = raw_file if isinstance(raw_file, list) else [raw_file]
        for path in paths:
            if not path or not isinstance(path, str): continue
            url = path if path.startswith("http") else BASE_URL + path
            if "." in url.split("/")[-1]: grouped.setdefault(lang, []).append(url)
    return grouped

def lm_get_streams(play_url: str, item_type: str, ep_id: str = None) -> Dict:
    _empty = {"streams": {}, "subtitles": {}}
    page_url = play_url.split("#")[0]
    html = fetch_text(page_url, timeout=25, headers={"Referer": BASE_URL + "/"})
    if not html: return _empty

    api_headers = {"Referer": play_url, "Origin": BASE_URL, "X-Requested-With": "XMLHttpRequest"}

    if item_type == "Movie":
        storage = _extract_storage(html, "movie_storage")
        mid, h, exp = storage.get("id_movie"), storage.get("hash"), storage.get("expires")
        if not mid:
            m = re.search(r"id_movie\s*[:=]\s*['\"]?(\d+)['\"]?", html)
            if m: mid = m.group(1)
        if not all([mid, h, exp]): return _empty
        api_url = f"{BASE_URL}/api/v1/security/movie-access?id_movie={mid}&hash={h}&expires={exp}"
    else:
        storage = _extract_storage(html, "show_storage")
        h, exp = storage.get("hash"), storage.get("expires")
        if not all([ep_id, h, exp]): return _empty
        api_url = f"{BASE_URL}/api/v1/security/episode-access?id_episode={ep_id}&hash={h}&expires={exp}"

    data = fetch_json(api_url, headers=api_headers)
    if not data or not data.get("success"): return _empty
    
    streams = {k: (v if v.startswith("http") else BASE_URL + v) for k, v in data.get("streams", {}).items() if v}
    return {"streams": streams, "subtitles": _parse_subtitles(data.get("subtitles", []))}

def lm_get_show_id_from_page(url: str) -> Optional[int]:
    html = fetch_text(url, headers={"Referer": BASE_URL + "/"})
    if not html: return None
    for pat in [r"id_show\s*[:=]\s*['\"]?(\d+)['\"]?", r"data-id=['\"](\d+)['\"]"]:
        m = re.search(pat, html)
        if m: return int(m.group(1))
    return None

def _parse_quality_to_int(label: str) -> int:
    try: return int(str(label).strip().rstrip("p"))
    except: return 0

def _extract_slug(path: str) -> Optional[str]:
    m = re.search(r"/(?:movies|shows)/(?:view|play)/([^/?#]+)", path)
    return m.group(1) if m else None

def _encode_play_data(item_type: str, play_url: str, ep_id: str = None) -> str:
    return f"movie::{play_url}" if item_type == "Movie" else f"show::{play_url}::{ep_id or ''}"

def _decode_play_data(pd: str) -> Tuple[str, str, Optional[str]]:
    if pd.startswith("movie::"): return "Movie", pd[7:], None
    if pd.startswith("show::"):
        pts = pd.split("::", 2)
        return "Show", pts[1], pts[2] if len(pts) > 2 else None
    return ("Show" if "/shows/" in pd else "Movie"), pd, (re.search(r"#S\d+-E\d+-(\d+)", pd).group(1) if "#S" in pd else None)

# =====================================================================
# API Models
# =====================================================================

class SearchRequest(BaseModel): query: str
class LinksRequest(BaseModel): url: str; season: Optional[int] = None; episode: Optional[int] = None; id: Optional[int] = None
class ExtractRequest(BaseModel): hubdrive_links: List[str]
class StreamResult(BaseModel): server: str; url: str; quality: int = 0; label: str = ""; type: str = "m3u8"; subtitles: Dict = {}
class ExtractedResponse(BaseModel): status: str; total_links: int; results: List[StreamResult]

# =====================================================================
# FastAPI
# =====================================================================

app = FastAPI(title="Lumino LookMovie2 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def root(): return {"message": "Lumino LookMovie2 API", "status": "running"}

@app.get("/api/debug")
async def api_debug(q: str = "avengers"):
    url = f"{BASE_URL}/api/v1/movies/do-search/?q={urllib.parse.quote_plus(q)}"
    return {"url": url, "result": fetch_json(url)}

@app.post("/api/search")
async def api_search(req: SearchRequest):
    loop = asyncio.get_event_loop()
    m = await loop.run_in_executor(None, lm_search_movies, req.query)
    s = await loop.run_in_executor(None, lm_search_shows, req.query)
    return {"status": "success", "results": m + s}

@app.post("/api/get-links")
async def api_get_links(req: LinksRequest):
    slug = _extract_slug(urlparse(req.url).path)
    if not slug: raise HTTPException(400, "Invalid URL")
    is_show = "/shows/" in req.url
    loop = asyncio.get_event_loop()
    if not is_show:
        play_url = lm_movie_play_url(slug)
        return {"status": "success", "hubdrive_links": [_encode_play_data("Movie", play_url)]}
    id_show = req.id or await loop.run_in_executor(None, lm_get_show_id_from_page, req.url)
    if not id_show: raise HTTPException(404, "Show ID not found")
    if req.season is None:
        eps = await loop.run_in_executor(None, lm_get_episode_list, id_show)
        return {"status": "success", "episodes": eps}
    play_url, ep_id = await loop.run_in_executor(None, lm_show_play_url, slug, id_show, req.season, req.episode)
    return {"status": "success", "hubdrive_links": [_encode_play_data("Show", play_url, ep_id)]}

def _resolve_single(pd: str) -> List[Dict]:
    try:
        t, url, ep_id = _decode_play_data(pd)
        res = lm_get_streams(url, t, ep_id)
        # Prioritize 'auto' or 'master' playlists as they usually work best
        order = ["auto", "master", "1080p", "1080", "720p", "720", "480p", "480"]
        
        results = []
        for k, v in res["streams"].items():
            results.append({
                "server": "LookMovie2",
                "url": v,
                "quality": _parse_quality_to_int(k),
                "label": "Auto" if k in ["auto", "master"] else (f"{k}p" if str(k).isdigit() else k),
                "type": "m3u8",
                "subtitles": res["subtitles"]
            })
            
        # Sort results based on the defined order
        results.sort(key=lambda x: order.index(x["label"].replace("p", "").lower()) if x["label"].replace("p", "").lower() in order else (order.index("auto") if x["label"] == "Auto" else 99))
        
        logger.debug(f"[_resolve_single] Resolved {len(results)} streams for {url!r}")
        return results
    except Exception: return []

@app.post("/api/extract", response_model=ExtractedResponse)
async def api_extract(req: ExtractRequest):
    all_res = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(_resolve_single, pd) for pd in req.hubdrive_links]
        for f in concurrent.futures.as_completed(futures): all_res.extend(f.result())
    return {"status": "success", "total_links": len(all_res), "results": all_res}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)