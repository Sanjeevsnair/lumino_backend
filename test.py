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

from bs4 import BeautifulSoup
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
# Set these in HF Spaces → Settings → Repository secrets / Variables:
#
#   FLARESOLVERR_URL  e.g. http://my-flaresolverr:8191
#       Routes every LookMovie2 request through a FlareSolverr instance
#       (real headless Chrome that solves Cloudflare JS challenges).
#       Free self-host: https://github.com/FlareSolverr/FlareSolverr
#
#   HTTP_PROXY        e.g. http://user:pass@residential-proxy:8080
#                      or  socks5://user:pass@proxy:1080
#       Threads all requests through a proxy (residential IPs bypass CF).
# =====================================================================

FLARE_URL  = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")
HTTP_PROXY = os.environ.get("HTTP_PROXY", "") or os.environ.get("HTTPS_PROXY", "")
RELAY_URL  = os.environ.get("RELAY_URL", "").rstrip("/")
# RELAY_URL: any URL that accepts GET /?url=<encoded_target_url> and returns its body.
# Free option — deploy this 5-line Cloudflare Worker (free tier, 100k req/day):
#
#   export default {
#     async fetch(request) {
#       const target = new URL(request.url).searchParams.get('url');
#       if (!target) return new Response('missing ?url=', {status: 400});
#       const resp = await fetch(target, { headers: { 'User-Agent': request.headers.get('User-Agent') || 'Mozilla/5.0' }});
#       return new Response(await resp.text(), { status: resp.status, headers: { 'Content-Type': resp.headers.get('Content-Type') || 'text/plain' }});
#     }
#   };
#
# Deploy at: https://dash.cloudflare.com/ → Workers → Create Worker → paste above → Deploy
# Then set RELAY_URL = https://your-worker.your-subdomain.workers.dev  in HF Spaces Variables

_PROXIES = {"http": HTTP_PROXY, "https": HTTP_PROXY} if HTTP_PROXY else {}

# =====================================================================
# HTTP Layer  (FlareSolverr → curl_cffi → requests → cloudscraper → httpx)
# Needed to bypass Cloudflare on LookMovie2.
# =====================================================================

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _relay_get(url: str, timeout: int = 25, headers: dict = None):
    """
    Fetch url via a relay/proxy worker (e.g. a Cloudflare Worker).
    The relay must accept: GET {RELAY_URL}/?url=<encoded_url>
    and return the response body directly.
    Activated when RELAY_URL env var is set.
    """
    if not RELAY_URL:
        return None
    try:
        import httpx
        relay_request_url = f"{RELAY_URL}/?url={urllib.parse.quote(url, safe='')}"
        h = {
            **_BASE_HEADERS,
            **(headers or {}),
            # strip headers the relay shouldn't forward
        }
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
            resp = client.get(relay_request_url, headers=h)
            logger.debug(f"[http:relay] {url!r} via relay → HTTP {resp.status_code}")
            return resp
    except Exception as e:
        logger.debug(f"[http:relay] {url!r} failed: {e}")
    return None


def _flaresolverr_get(url: str, timeout: int = 60, headers: dict = None):
    """Route through FlareSolverr (headless Chrome). Requires FLARESOLVERR_URL env var."""
    if not FLARE_URL:
        return None
    try:
        import requests as _req
        payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}
        r = _req.post(f"{FLARE_URL}/v1", json=payload,
                      timeout=timeout + 10, proxies=_PROXIES or None)
        data = r.json()
        if data.get("status") == "ok":
            sol = data["solution"]
            class _FakeResp:
                status_code = sol.get("status", 200)
                text = sol.get("response", "")
            return _FakeResp()
        logger.debug(f"[http:flaresolverr] non-ok for {url!r}: {data.get('message')}")
    except Exception as e:
        logger.debug(f"[http:flaresolverr] {url!r} failed: {e}")
    return None


# ── curl_cffi (TLS fingerprint impersonation) ────────────────────────
_cffi_session = None
_cffi_lock = threading.Lock()

def _get_cffi_session():
    global _cffi_session
    if _cffi_session is None:
        try:
            from curl_cffi import requests as cffi_requests
            _cffi_session = cffi_requests.Session(impersonate="chrome124")
            logger.info("[http] curl_cffi session initialised (chrome124)")
        except Exception as e:
            logger.warning(f"[http] curl_cffi unavailable: {e}")
    return _cffi_session


def _cffi_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        with _cffi_lock:
            session = _get_cffi_session()
            if session is None:
                return None
            kwargs = dict(timeout=timeout, allow_redirects=True,
                          headers={**_BASE_HEADERS, **(headers or {})})
            if HTTP_PROXY:
                kwargs["proxies"] = _PROXIES
            return session.get(url, **kwargs)
    except Exception as e:
        logger.debug(f"[http:cffi] {url!r} → {e}")
        return None


# ── plain requests (most reliable on Linux / HF Spaces) ─────────────
_req_session: Optional[Any] = None
_req_lock = threading.Lock()

def _get_req_session():
    global _req_session
    if _req_session is None:
        try:
            import requests
            s = requests.Session()
            s.headers.update(_BASE_HEADERS)
            if HTTP_PROXY:
                s.proxies.update(_PROXIES)
            _req_session = s
            logger.info("[http] requests session initialised")
        except Exception as e:
            logger.warning(f"[http] requests unavailable: {e}")
    return _req_session


def _requests_get(url: str, timeout: int = 25, headers: dict = None):
    """Plain requests — most compatible on Linux containers."""
    try:
        with _req_lock:
            session = _get_req_session()
            if session is None:
                return None
            h = {**_BASE_HEADERS, **(headers or {})}
            return session.get(url, timeout=timeout, allow_redirects=True,
                               headers=h, verify=True)
    except Exception as e:
        logger.debug(f"[http:requests] {url!r} → {e}")
        # SSL error? retry without verification
        try:
            import requests as _req
            h = {**_BASE_HEADERS, **(headers or {})}
            return _req.get(url, timeout=timeout, allow_redirects=True,
                            headers=h, verify=False,
                            proxies=_PROXIES or None)
        except Exception as e2:
            logger.debug(f"[http:requests-noverify] {url!r} → {e2}")
        return None


# ── cloudscraper ─────────────────────────────────────────────────────
def _cloud_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        import cloudscraper
        sc = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "mobile": False}
        )
        h = {**_BASE_HEADERS, **(headers or {})}
        sc.headers.update(h)
        return sc.get(url, timeout=timeout, allow_redirects=True,
                      proxies=_PROXIES or None, verify=True)
    except Exception as e:
        logger.debug(f"[http:cloudscraper] {url!r} → {e}")
        return None


# ── httpx ────────────────────────────────────────────────────────────
def _httpx_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        import httpx
        h = {**_BASE_HEADERS, **(headers or {})}
        proxy_url = HTTP_PROXY or None
        with httpx.Client(http2=True, headers=h, timeout=timeout,
                          follow_redirects=True, proxy=proxy_url,
                          verify=True) as client:
            return client.get(url)
    except Exception as e:
        logger.debug(f"[http:httpx] {url!r} → {e}")
        # Retry without SSL verification
        try:
            import httpx
            h = {**_BASE_HEADERS, **(headers or {})}
            with httpx.Client(http2=False, headers=h, timeout=timeout,
                              follow_redirects=True, verify=False) as client:
                return client.get(url)
        except Exception as e2:
            logger.debug(f"[http:httpx-noverify] {url!r} → {e2}")
        return None


def fetch_text(url: str, timeout: int = 25, headers: dict = None) -> Optional[str]:
    """Fetch using best available strategy.
    Chain: relay → FlareSolverr → curl_cffi → requests → cloudscraper → httpx
    """
    strategies = [
        ("relay",        _relay_get        if RELAY_URL  else None),
        ("flaresolverr", _flaresolverr_get if FLARE_URL  else None),
        ("cffi",         _cffi_get),
        ("requests",     _requests_get),
        ("cloudscraper", _cloud_get),
        ("httpx",        _httpx_get),
    ]
    for name, fn in strategies:
        if fn is None:
            continue
        try:
            resp = fn(url, timeout, headers)
            if resp is not None and resp.status_code == 200:
                logger.debug(f"[fetch_text] {name} succeeded for {url!r}")
                return resp.text
            if resp is not None:
                logger.debug(f"[fetch_text] {name} → HTTP {resp.status_code} for {url!r}")
        except Exception as e:
            logger.debug(f"[fetch_text] {name} exception for {url!r}: {e}")
        if name == "cffi":
            time.sleep(0.3)
    logger.warning(f"[fetch_text] ALL strategies failed for {url!r}")
    return None


def fetch_json(url: str, timeout: int = 20) -> Optional[Any]:
    """Fetch JSON from LookMovie2 internal API."""
    hdrs = {
        "Accept": "application/json, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_URL + "/",
    }
    text = fetch_text(url, timeout=timeout, headers=hdrs)
    if text:
        try:
            return json.loads(text)
        except Exception:
            pass
    return None

# =====================================================================
# LookMovie2 — Search
# =====================================================================

def lm_search_movies(query: str, max_results: int = 10) -> List[Dict]:
    q = urllib.parse.quote_plus(query.strip())
    data = fetch_json(f"{BASE_URL}/api/v1/movies/do-search/?q={q}")
    if not data or "result" not in data:
        return []
    results = []
    for item in data["result"][:max_results]:
        results.append({
            "type":    "Movie",
            "id":      item.get("id_movie"),
            "slug":    item.get("slug", ""),
            "title":   item.get("title", "?"),
            "year":    str(item.get("year", "N/A")),
            "rating":  str(item.get("imdb_rating", "N/A")),
            "quality": item.get("quality_badge", "N/A"),
            "url":     f"{BASE_URL}/movies/view/{item.get('slug', '')}",
            "poster":  None,
            "source":  "lookmovie",
        })
    return results


def lm_search_shows(query: str, max_results: int = 10) -> List[Dict]:
    q = urllib.parse.quote_plus(query.strip())
    data = fetch_json(f"{BASE_URL}/api/v1/shows/do-search/?q={q}")
    if not data or "result" not in data:
        return []
    results = []
    for item in data["result"][:max_results]:
        results.append({
            "type":    "Show",
            "id":      item.get("id_show"),
            "slug":    item.get("slug", ""),
            "title":   item.get("title", "?"),
            "year":    str(item.get("year", "N/A")),
            "rating":  str(item.get("imdb_rating", "N/A")),
            "quality": item.get("quality_badge", "N/A"),
            "url":     f"{BASE_URL}/shows/view/{item.get('slug', '')}",
            "poster":  None,
            "source":  "lookmovie",
        })
    return results

# =====================================================================
# LookMovie2 — Episode List
# =====================================================================

def lm_get_episode_list(id_show: int) -> Dict[str, Dict[str, str]]:
    """
    Returns nested dict: { "1": { "1": episode_id, "2": episode_id, ... }, "2": {...} }
    Keys are strings; values are episode ID strings.
    """
    data = fetch_json(f"{BASE_URL}/api/v2/download/episode/list?id={id_show}")
    if not data or "list" not in data:
        return {}
    result: Dict[str, Dict[str, str]] = {}
    for season_num, episodes in data["list"].items():
        result[str(season_num)] = {
            str(ep_num): str(ep_data.get("id_episode", ""))
            for ep_num, ep_data in episodes.items()
        }
    return result

# =====================================================================
# LookMovie2 — Play URL builders
# =====================================================================

def lm_movie_play_url(slug: str) -> str:
    return f"{BASE_URL}/movies/play/{slug}"


def lm_show_play_url(
    slug: str, id_show: int, season: int, episode: int
) -> Tuple[str, str]:
    """
    Returns (play_url, episode_id_str).
    Raises ValueError if season/episode not found.
    """
    episodes = lm_get_episode_list(id_show)
    s = str(season)
    e = str(episode)
    if s not in episodes:
        avail = ", ".join(sorted(episodes.keys(), key=int))
        raise ValueError(f"Season {season} not found. Available seasons: {avail}")
    if e not in episodes[s]:
        avail = ", ".join(sorted(episodes[s].keys(), key=int))
        raise ValueError(
            f"Episode {episode} not in season {season}. Available episodes: {avail}"
        )
    episode_id = episodes[s][e]
    play_url = f"{BASE_URL}/shows/play/{slug}#S{season}-E{episode}-{episode_id}"
    return play_url, episode_id

# =====================================================================
# LookMovie2 — Stream & Subtitle Resolver
# =====================================================================

def _extract_storage(html: str, storage_name: str) -> Dict:
    """
    Parse window['movie_storage'] = { ... } or window['show_storage'] = { ... }
    from play-page HTML. Returns scalar dict of parsed key-value pairs.
    Uses a bracket-counter approach to correctly handle nested objects.
    """
    # Find the start of the assignment
    pat_start = re.compile(
        rf"window\['{re.escape(storage_name)}'\]\s*=\s*\{{",
        re.S
    )
    m = pat_start.search(html)
    if not m:
        logger.debug(f"[_extract_storage] '{storage_name}' not found in HTML")
        return {}

    # Walk forward counting braces to find the matching closing brace
    start = m.end() - 1  # position of the opening '{'
    depth = 0
    end = start
    for i in range(start, len(html)):
        if html[i] == '{':
            depth += 1
        elif html[i] == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        logger.debug(f"[_extract_storage] unmatched braces for '{storage_name}'")
        return {}

    block = html[start + 1:end]   # content between the outer { }
    result: Dict = {}
    for km in re.finditer(
        r"(\w+)\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|([-\d]+))", block
    ):
        key = km.group(1)
        val = km.group(2) or km.group(3)
        if val is None and km.group(4) is not None:
            val = int(km.group(4))
        result[key] = val
    logger.debug(f"[_extract_storage] '{storage_name}' keys: {list(result.keys())}")
    return result


def _parse_subtitles(raw_list: list) -> Dict[str, List[str]]:
    """
    Convert API subtitle list into grouped dict {"English": ["https://..."], ...}.

    Handles two API formats from LookMovie2:
      - Movies:  {"language": "English", "file": "/storage6/subs/..."}
      - Shows:   {"language": "English", "file": [id, id, "lang", "title", "/storage6/.vtt", ...]}
                 The list mixes integers, language codes, titles, and actual VTT paths.
    """
    grouped: Dict[str, List[str]] = {}
    for entry in raw_list or []:
        lang = entry.get("language", "Unknown")
        raw_file = entry.get("file", "")
        if not raw_file:
            continue
        # Normalise to list (shows send a mixed list, movies send a plain string)
        paths = raw_file if isinstance(raw_file, list) else [raw_file]
        for path in paths:
            if not path or not isinstance(path, str):
                continue   # skip numeric IDs and None
            # Only keep actual subtitle file paths (VTT/SRT/ASS) or full URLs
            if path.startswith("http"):
                grouped.setdefault(lang, []).append(path)
            elif path.startswith("/") and "." in path.split("/")[-1]:
                # e.g. /storage8/.../en_abc.vtt  - has a file extension
                grouped.setdefault(lang, []).append(BASE_URL + path)
            # skip language codes, titles, numeric IDs etc.
    return grouped


def lm_get_streams(
    play_url: str, item_type: str, episode_id: Optional[str] = None
) -> Dict:
    """
    Fetch the play page, extract storage hash/expires, call the security API,
    return {"streams": {quality_label: m3u8_url}, "subtitles": {lang: [url]}}.
    """
    _empty: Dict = {"streams": {}, "subtitles": {}}
    page_url = play_url.split("#")[0]

    logger.info(f"[lm_get_streams] fetching play page: {page_url}")
    html = fetch_text(page_url, timeout=25, headers={"Referer": BASE_URL + "/"})
    if not html:
        logger.warning("[lm_get_streams] could not fetch play page")
        return _empty

    if item_type == "Movie":
        storage = _extract_storage(html, "movie_storage")
        id_movie = storage.get("id_movie")
        hash_val = storage.get("hash")
        expires  = storage.get("expires")
        if not all([id_movie, hash_val, expires]):
            logger.warning(f"[lm_get_streams] incomplete movie_storage: {storage}")
            return _empty
        api_url = (
            f"{BASE_URL}/api/v1/security/movie-access"
            f"?id_movie={id_movie}&hash={hash_val}&expires={expires}"
        )
    else:  # Show
        storage = _extract_storage(html, "show_storage")
        hash_val = storage.get("hash")
        expires  = storage.get("expires")
        logger.info(
            f"[lm_get_streams] show_storage → hash={hash_val!r} "
            f"expires={expires!r} episode_id={episode_id!r}"
        )
        if not episode_id:
            logger.warning("[lm_get_streams] episode_id is missing — cannot call episode-access API")
            return _empty
        if not hash_val or not expires:
            # Scan the HTML for all window['...'] = { patterns to debug the actual variable name
            all_window_vars = re.findall(r"window\['([^']+)'\]\s*=", html)
            logger.warning(
                f"[lm_get_streams] show_storage missing hash/expires. "
                f"All window[...] vars found in HTML: {all_window_vars}\n"
                f"Full storage dict: {storage}\n"
                f"HTML snippet (first 3000 chars):\n{html[:3000]}"
            )
            return _empty
        api_url = (
            f"{BASE_URL}/api/v1/security/episode-access"
            f"?id_episode={episode_id}&hash={hash_val}&expires={expires}"
        )

    logger.info(f"[lm_get_streams] calling security API: {api_url}")
    data = fetch_json(api_url)
    if not data or not data.get("success"):
        logger.warning(f"[lm_get_streams] security API failed: {data}")
        return _empty

    return {
        "streams":   {k: v for k, v in data.get("streams", {}).items() if v},
        "subtitles": _parse_subtitles(data.get("subtitles", [])),
    }

# =====================================================================
# LookMovie2 — Show ID extraction from view page
# =====================================================================

def lm_get_show_id_from_page(url: str) -> Optional[int]:
    """Fetch the show view page and extract id_show from embedded JS or data attributes."""
    html = fetch_text(url, timeout=20, headers={"Referer": BASE_URL + "/"})
    if not html:
        return None

    # Typical patterns in the page JS
    for pat in (
        r"id_show\s*[:=]\s*['\"]?(\d+)['\"]?",
        r"\"id_show\"\s*:\s*(\d+)",
        r"'id_show'\s*:\s*(\d+)",
        r"data-id=['\"](\d+)['\"]",
    ):
        m = re.search(pat, html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass

    # Fallback: soup data-id attribute
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find(attrs={"data-id": True})
    if el:
        try:
            return int(el["data-id"])
        except (ValueError, TypeError):
            pass

    return None

# =====================================================================
# Helpers
# =====================================================================

def _parse_quality_to_int(label: str) -> int:
    """'1080p' → 1080, '720' → 720, 'unknown' → 0"""
    s = str(label).strip().rstrip("p")
    try:
        return int(s)
    except ValueError:
        return 0


def _extract_slug(path: str) -> Optional[str]:
    m = re.search(r"/(?:movies|shows)/(?:view|play)/([^/?#]+)", path)
    return m.group(1) if m else None


def _encode_play_data(item_type: str, play_url: str, episode_id: Optional[str] = None) -> str:
    """
    Encode a play URL + metadata into an opaque string for the extract endpoint.
    Format:  "movie::{play_url}"
             "show::{play_url}::{episode_id}"
    """
    if item_type == "Movie":
        return f"movie::{play_url}"
    return f"show::{play_url}::{episode_id or ''}"


def _decode_play_data(play_data: str) -> Tuple[str, str, Optional[str]]:
    """
    Decode encoded play_data string back to (item_type, play_url, episode_id).
    Falls back to heuristic parsing for raw URLs.
    """
    if play_data.startswith("movie::"):
        return "Movie", play_data[len("movie::"):], None

    if play_data.startswith("show::"):
        parts = play_data.split("::", 2)
        play_url  = parts[1] if len(parts) > 1 else ""
        episode_id = parts[2] if len(parts) > 2 else None
        return "Show", play_url, episode_id or None

    # Raw URL fallback
    item_type = "Show" if "/shows/" in play_data else "Movie"
    frag = re.search(r"#S\d+-E\d+-(\d+)", play_data)
    episode_id = frag.group(1) if frag else None
    return item_type, play_data, episode_id

# =====================================================================
# Pydantic models
# =====================================================================

class SearchRequest(BaseModel):
    query: str


class LinksRequest(BaseModel):
    url:     str
    season:  Optional[int] = None
    episode: Optional[int] = None
    id:      Optional[int] = None   # show/movie numeric ID from search results


class ExtractRequest(BaseModel):
    hubdrive_links: List[str]       # list of encoded play_data strings


class StreamResult(BaseModel):
    server:    str
    url:       str
    quality:   Optional[int]  = None
    label:     Optional[str]  = None
    type:      Optional[str]  = None
    subtitles: Optional[Dict] = None


class ExtractedResponse(BaseModel):
    status:      str
    total_links: int
    results:     List[StreamResult]

# =====================================================================
# FastAPI app
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== Lumino LookMovie2 API starting ===")
    logger.info(f"Provider base URL : {BASE_URL}")
    logger.info(f"Relay URL         : {RELAY_URL or '(not set)'}")
    logger.info(f"FlareSolverr      : {FLARE_URL or '(not set)'}")
    logger.info(f"HTTP Proxy        : {HTTP_PROXY or '(not set)'}")
    yield
    logger.info("=== Lumino LookMovie2 API shutdown ===")


app = FastAPI(
    title="Lumino — LookMovie2 API",
    description=(
        "Stream resolver for LookMovie2. "
        "Endpoints: /api/search · /api/get-links · /api/extract"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================================
# Root / Health
# =====================================================================

@app.get("/")
async def root():
    return {"message": "Lumino LookMovie2 API", "status": "running", "base_url": BASE_URL}


@app.get("/health")
async def health():
    return {
        "status":         "healthy",
        "provider":       "lookmovie2",
        "base_url":       BASE_URL,
        "relay":          RELAY_URL or None,
        "flaresolverr":   FLARE_URL or None,
        "proxy":          bool(HTTP_PROXY),
    }


# =====================================================================
# /api/debug  — diagnose Cloudflare issues from hosted environments
# =====================================================================

@app.get("/api/debug")
async def api_debug(q: str = "avengers"):
    """
    Probe every HTTP strategy against LookMovie2 and return raw results.
    Use from HF Spaces to diagnose what is being blocked:
      GET /api/debug?q=prakambanam
    """
    url = f"{BASE_URL}/api/v1/movies/do-search/?q={urllib.parse.quote_plus(q)}"
    hdrs = {
        "Accept": "application/json, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_URL + "/",
    }

    strategies = [
        ("relay",        _relay_get        if RELAY_URL  else None),
        ("flaresolverr", _flaresolverr_get if FLARE_URL  else None),
        ("cffi",         _cffi_get),
        ("requests",     _requests_get),
        ("cloudscraper", _cloud_get),
        ("httpx",        _httpx_get),
    ]

    results = {}
    for name, fn in strategies:
        if fn is None:
            results[name] = {"skipped": True, "reason": "not configured"}
            continue
        try:
            resp = fn(url, 20, hdrs)
            results[name] = {
                "status":       resp.status_code if resp else None,
                "body_preview": resp.text[:400] if resp else None,
                "error":        None,
            }
        except Exception as e:
            results[name] = {"status": None, "body_preview": None, "error": str(e)}

    return {
        "url":              url,
        "relay":            RELAY_URL or None,
        "flaresolverr":     FLARE_URL or None,
        "proxy":            HTTP_PROXY or None,
        "strategy_results": results,
    }


# =====================================================================
# /api/search
# =====================================================================

@app.post("/api/search")
async def api_search(request: SearchRequest):
    """
    Search LookMovie2 for movies and shows.

    Returns combined list sorted movies-first, then shows.
    Each result includes: type, id, slug, title, year, rating, quality, url, poster, source.
    """
    logger.info(f"[api_search] query={request.query!r}")

    loop = asyncio.get_event_loop()
    try:
        # Run sequentially — concurrent cffi calls race on the TLS session and cause SSL errors
        movies = await loop.run_in_executor(None, lm_search_movies, request.query)
        shows  = await loop.run_in_executor(None, lm_search_shows,  request.query)
    except Exception as e:
        logger.error(f"[api_search] error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {e}",
        )

    results = movies + shows
    return {"status": "success", "count": len(results), "results": results}

# =====================================================================
# /api/get-links
# =====================================================================

@app.post("/api/get-links")
async def api_get_links(request: LinksRequest):
    """
    Given a LookMovie2 view URL, return encoded play-data string(s).

    Movies:
      • Returns hubdrive_links: ["movie::{play_url}"]

    Shows (season + episode required):
      • Resolves the specific episode, returns hubdrive_links: ["show::{play_url}::{episode_id}"]

    Shows (no season/episode):
      • Returns the full episode map so the client can pick an episode.
        hubdrive_links will be empty; use the 'episodes' field instead.

    Pass the numeric 'id' field from /api/search results to skip an extra
    page fetch for shows.
    """
    if not request.url:
        raise HTTPException(status_code=400, detail="url is required")

    url    = request.url.strip()
    parsed = urlparse(url)
    path   = parsed.path

    is_show  = "/shows/"  in path
    is_movie = "/movies/" in path

    if not (is_show or is_movie):
        raise HTTPException(
            status_code=400,
            detail="URL must be a LookMovie2 /movies/ or /shows/ URL",
        )

    slug = _extract_slug(path)
    if not slug:
        raise HTTPException(status_code=400, detail="Could not extract slug from URL")

    loop = asyncio.get_event_loop()

    try:
        # ── Movie ──────────────────────────────────────────────────────
        if is_movie:
            play_url = lm_movie_play_url(slug)
            metadata = {
                "title": slug.replace("-", " ").title(),
                "type":  "Movie",
                "url":   url,
                "slug":  slug,
            }
            return {
                "status":        "success",
                "metadata":      metadata,
                "hubdrive_links": [_encode_play_data("Movie", play_url)],
                "total_links":   1,
                "source":        "lookmovie",
            }

        # ── Show ───────────────────────────────────────────────────────
        # Resolve id_show: prefer caller-supplied id, else fetch from page
        id_show: Optional[int] = request.id
        if id_show is None:
            logger.info(f"[api_get_links] fetching show page to extract id_show: {url}")
            id_show = await loop.run_in_executor(None, lm_get_show_id_from_page, url)
            if id_show is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "Could not determine show ID from the page. "
                        "Pass the 'id' field from /api/search results."
                    ),
                )

        base_metadata = {
            "title":   slug.replace("-", " ").title(),
            "type":    "TvSeries",
            "url":     url,
            "slug":    slug,
            "id_show": id_show,
        }

        # No season/episode → return episode map
        if request.season is None or request.episode is None:
            logger.info(f"[api_get_links] fetching episode list for id_show={id_show}")
            episodes = await loop.run_in_executor(None, lm_get_episode_list, id_show)
            if not episodes:
                raise HTTPException(
                    status_code=404,
                    detail=f"No episodes found for show id={id_show}",
                )
            return {
                "status":        "success",
                "metadata":      base_metadata,
                "episodes":      episodes,           # { season: { episode: id } }
                "hubdrive_links": [],
                "total_links":   0,
                "source":        "lookmovie",
            }

        # Season + episode supplied → resolve play URL
        logger.info(
            f"[api_get_links] resolving S{request.season}E{request.episode} "
            f"for slug={slug!r} id_show={id_show}"
        )
        play_url, episode_id = await loop.run_in_executor(
            None, lm_show_play_url, slug, id_show, request.season, request.episode
        )

        metadata = {
            **base_metadata,
            "season":  request.season,
            "episode": request.episode,
        }
        return {
            "status":        "success",
            "metadata":      metadata,
            "hubdrive_links": [_encode_play_data("Show", play_url, episode_id)],
            "total_links":   1,
            "source":        "lookmovie",
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"[api_get_links] error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get links: {e}",
        )

# =====================================================================
# /api/extract  — resolve encoded play_data → m3u8 streams
# =====================================================================

def _resolve_single(play_data: str) -> List[Dict]:
    """
    Worker: decode play_data, call lm_get_streams, return list of stream dicts.
    """
    try:
        item_type, play_url, episode_id = _decode_play_data(play_data)
    except Exception as e:
        logger.error(f"[extract] _decode_play_data failed for {play_data[:80]!r}: {e}", exc_info=True)
        return []

    logger.info(
        f"[extract] resolving type={item_type!r} "
        f"play_url={play_url[:80]!r} episode_id={episode_id!r}"
    )

    try:
        result = lm_get_streams(play_url, item_type, episode_id)
    except Exception as e:
        logger.error(f"[extract] lm_get_streams failed: {e}", exc_info=True)
        return []

    streams   = result.get("streams",   {})
    subtitles = result.get("subtitles", {})
    logger.info(f"[extract] got {len(streams)} stream(s) for {play_url[:80]!r}")

    # Quality ordering (best first)
    order = ["1080p", "1080", "720p", "720", "480p", "480", "360p", "360"]

    def _sort_key(label: str) -> int:
        s = str(label)
        try:
            return order.index(s)
        except ValueError:
            return 99

    out: List[Dict] = []
    for quality_label, m3u8_url in sorted(streams.items(), key=lambda kv: _sort_key(str(kv[0]))):
        if not m3u8_url:
            continue
        q_int = _parse_quality_to_int(str(quality_label))
        human_label = (
            f"{quality_label}p"
            if str(quality_label).isdigit()
            else str(quality_label)
        )
        out.append({
            "server":    "LookMovie2",
            "url":       m3u8_url,
            "quality":   q_int,
            "label":     human_label,
            "type":      "m3u8",
            "subtitles": subtitles,   # grouped by language
        })

    return out


@app.post("/api/extract", response_model=ExtractedResponse)
async def api_extract(req: ExtractRequest):
    """
    Resolve encoded play_data strings (from /api/get-links) into actual m3u8 stream URLs.

    Accepts the same 'hubdrive_links' field name for API compatibility.
    Each entry should be an encoded string returned by /api/get-links.

    Returns per-quality m3u8 URLs together with subtitle tracks grouped by language.
    """
    if not req.hubdrive_links:
        raise HTTPException(status_code=400, detail="No links provided")

    play_data_list = req.hubdrive_links
    all_results: List[Dict] = []
    loop = asyncio.get_event_loop()

    max_workers = min(len(play_data_list), 6) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_resolve_single, pd): pd
            for pd in play_data_list
        }
        for fut in concurrent.futures.as_completed(futures):
            play_data = futures[fut]
            try:
                streams = fut.result()
                all_results.extend(streams)
            except Exception as e:
                logger.error(
                    f"[api_extract] worker failed for {play_data[:60]!r}: {e}",
                    exc_info=True
                )

    if not all_results:
        logger.warning("[api_extract] no streams resolved")

    return ExtractedResponse(
        status="success",
        total_links=len(all_results),
        results=[StreamResult(**r) for r in all_results],
    )

# =====================================================================
# Entrypoint
# =====================================================================

if __name__ == "__main__":
    logger.info("Starting Lumino LookMovie2 API on port 7860")
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="info")