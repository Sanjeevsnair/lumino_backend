"""
lookmovies.py
─────────────────────────────────────────────────────────────────────────────
LookMovie2 interactive search + play-URL + m3u8 stream resolver.

Features:
  • Search movies AND shows via the site's JSON API (fast, no HTML scraping)
  • Select a result by number
  • Movies  → /movies/play/... URL + fetches index.m3u8 for all qualities
  • Shows   → asks season + episode, builds /shows/play/...#Sn-Em-<id> URL
              + fetches index.m3u8 for all available qualities

Install:
    pip install curl_cffi cloudscraper beautifulsoup4 "httpx[http2]"

Usage:
    python lookmovies.py              # interactive
    python lookmovies.py "avengers"   # one-shot search
    python lookmovies.py --home       # scrape homepage listing
"""

import json
import re
import sys
import time
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bs4 import BeautifulSoup

BASE_URL = "https://www.lookmovie2.to"
_W = 72  # console width


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP LAYER
# ═══════════════════════════════════════════════════════════════════════════

def _cffi_get(url: str, timeout: int = 25, headers: dict = None) -> "Response | None":
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests.get(
            url, impersonate="chrome124", timeout=timeout,
            allow_redirects=True, headers=headers or {},
        )
    except Exception:
        return None


def _cloud_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        import cloudscraper
        sc = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        if headers:
            sc.headers.update(headers)
        return sc.get(url, timeout=timeout, allow_redirects=True)
    except Exception:
        return None


def _httpx_get(url: str, timeout: int = 25, headers: dict = None):
    try:
        import httpx
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if headers:
            h.update(headers)
        with httpx.Client(http2=True, headers=h, timeout=timeout,
                          follow_redirects=True) as client:
            return client.get(url)
    except Exception:
        return None


def fetch_text(url: str, timeout: int = 25, headers: dict = None) -> str | None:
    """Returns response text, trying multiple strategies."""
    for fn in [_cffi_get, _cloud_get, _httpx_get]:
        try:
            resp = fn(url, timeout, headers)
            if resp and resp.status_code == 200:
                return resp.text
        except Exception:
            pass
    return None


def fetch_json(url: str, timeout: int = 20) -> dict | list | None:
    """Fetch JSON from the site's internal API."""
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


# ═══════════════════════════════════════════════════════════════════════════
#  SEARCH  (via JSON API — fast & clean)
# ═══════════════════════════════════════════════════════════════════════════

def search_movies_api(query: str, max_results: int = 10) -> list[dict]:
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
            "link":    f"{BASE_URL}/movies/view/{item.get('slug', '')}",
        })
    return results


def search_shows_api(query: str, max_results: int = 10) -> list[dict]:
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
            "link":    f"{BASE_URL}/shows/view/{item.get('slug', '')}",
        })
    return results


def search(query: str, max_results: int = 10) -> dict:
    """Search both movies and shows, return combined result dict."""
    movies = search_movies_api(query, max_results)
    shows  = search_shows_api(query, max_results)
    return {"query": query, "movies": movies, "shows": shows}


# ═══════════════════════════════════════════════════════════════════════════
#  PLAY URL RESOLVER
# ═══════════════════════════════════════════════════════════════════════════

def get_movie_play_url(item: dict) -> str:
    """Simple: /movies/view/ → /movies/play/"""
    return f"{BASE_URL}/movies/play/{item['slug']}"


def get_episode_list(id_show: int) -> dict:
    """
    Returns nested dict: { season_num: { ep_num: episode_id_str } }
    Uses /api/v2/download/episode/list?id={id_show}
    """
    data = fetch_json(f"{BASE_URL}/api/v2/download/episode/list?id={id_show}")
    if not data or "list" not in data:
        return {}
    result = {}
    for season_num, episodes in data["list"].items():
        result[str(season_num)] = {
            str(ep_num): ep_data.get("id_episode", "")
            for ep_num, ep_data in episodes.items()
        }
    return result


def get_show_play_url(item: dict, season: int, episode: int) -> tuple[str | None, str | None]:
    """
    Returns (play_url, episode_id_str) or (None, None) on error.
    """
    print(f"  Fetching episode list for \"{item['title']}\" ...")
    episodes = get_episode_list(item["id"])

    s = str(season)
    e = str(episode)

    if s not in episodes:
        avail = ", ".join(sorted(episodes.keys(), key=int))
        print(f"  [ERROR] Season {season} not found. Available seasons: {avail}")
        return None, None

    if e not in episodes[s]:
        avail = ", ".join(sorted(episodes[s].keys(), key=int))
        print(f"  [ERROR] Episode {episode} not in season {season}. Available episodes: {avail}")
        return None, None

    episode_id = str(episodes[s][e])
    url = f"{BASE_URL}/shows/play/{item['slug']}#S{season}-E{episode}-{episode_id}"
    return url, episode_id


# ═══════════════════════════════════════════════════════════════════════════
#  M3U8 STREAM RESOLVER
# ═══════════════════════════════════════════════════════════════════════════

def _extract_storage(html: str, storage_name: str) -> dict:
    """
    Parse window['movie_storage'] = { ... } or window['show_storage'] = { ... }
    from the play page HTML and return a plain dict of scalar values.
    """
    # Match the JS object literal assigned to the storage variable
    pat = rf"window\['{re.escape(storage_name)}'\]\s*=\s*\{{([^{{}}]+(?:\{{[^{{}}]*\}}[^{{}}]*)*)\}}"
    m = re.search(pat, html, re.S)
    if not m:
        return {}
    block = m.group(1)
    result = {}
    # Extract:  key: 'value'  or  key: "value"  or  key: number
    for km in re.finditer(r"(\w+)\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|([-\d]+))", block):
        key  = km.group(1)
        val  = km.group(2) or km.group(3)
        if val is None and km.group(4) is not None:
            val = int(km.group(4))
        result[key] = val
    return result


def get_streams(play_url: str, item_type: str, episode_id: str = None) -> dict:
    """
    Fetch the play page, extract hash+expires from movie_storage/show_storage,
    call the security API, and return streams dict  { quality_label: m3u8_url }.

    For movies: quality labels are '480p', '720p', '1080p'
    For shows:  quality labels are '480', '720', '1080'  (site convention)

    Returns a dict:
        {
            "streams":   { quality_label: m3u8_url, ... },
            "subtitles": { "English": [url1, url2, ...], "Spanish": [...], ... },
        }
    Returns {"streams": {}, "subtitles": {}} on any failure.
    """
    _empty = {"streams": {}, "subtitles": {}}

    # Fetch the play page (fragment is ignored by server, use the base path)
    page_url = play_url.split("#")[0]
    print(f"  Fetching play page for stream info ...")
    html = fetch_text(page_url, timeout=25, headers={"Referer": BASE_URL + "/"})
    if not html:
        print("  [WARN] Could not fetch play page.")
        return _empty

    api_headers = {
        "Referer": play_url,
        "Origin":  BASE_URL,
        "Accept":  "application/json, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _parse_subtitles(raw_list: list) -> dict:
        """
        Convert the API subtitle list:
            [{"language": "English", "file": "/storage6/..."}]
        into a grouped dict:
            {"English": ["https://...", "https://..."], "Spanish": [...]}
        Subtitle paths starting with '/' are relative to BASE_URL.
        """
        grouped: dict[str, list] = {}
        for entry in (raw_list or []):
            lang = entry.get("language", "Unknown")
            path = entry.get("file", "")
            if not path:
                continue
            url = path if path.startswith("http") else BASE_URL + path
            grouped.setdefault(lang, []).append(url)
        return grouped

    if item_type == "Movie":
        storage = _extract_storage(html, "movie_storage")
        id_movie = storage.get("id_movie")
        hash_val = storage.get("hash")
        expires  = storage.get("expires")
        if not all([id_movie, hash_val, expires]):
            print(f"  [WARN] movie_storage incomplete: {storage}")
            return _empty
        api_url = (
            f"{BASE_URL}/api/v1/security/movie-access"
            f"?id_movie={id_movie}&hash={hash_val}&expires={expires}"
        )
        data = fetch_json(api_url)
        if not data or not data.get("success"):
            print(f"  [WARN] movie-access API failed: {data}")
            return _empty
        return {
            "streams":   {k: v for k, v in data.get("streams", {}).items() if v},
            "subtitles": _parse_subtitles(data.get("subtitles", [])),
        }

    else:  # Show
        storage = _extract_storage(html, "show_storage")
        hash_val = storage.get("hash")
        expires  = storage.get("expires")
        if not all([hash_val, expires, episode_id]):
            print(f"  [WARN] show_storage incomplete: {storage}")
            return _empty
        api_url = (
            f"{BASE_URL}/api/v1/security/episode-access"
            f"?id_episode={episode_id}&hash={hash_val}&expires={expires}"
        )
        data = fetch_json(api_url)
        if not data or not data.get("success"):
            print(f"  [WARN] episode-access API failed: {data}")
            return _empty
        return {
            "streams":   {k: v for k, v in data.get("streams", {}).items() if v},
            "subtitles": _parse_subtitles(data.get("subtitles", [])),
        }


# ═══════════════════════════════════════════════════════════════════════════
#  HOMEPAGE SCRAPE  (HTML fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _parse_card_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for el in soup.find_all("div", class_="movie-item-style-2"):
        data = {}
        h6 = el.find("h6")
        if h6:
            a = h6.find("a")
            data["title"] = a.get_text(strip=True) if a else h6.get_text(strip=True)
        else:
            img = el.find("img")
            data["title"] = img.get("alt", "").strip() if img else ""
        if not data["title"]:
            continue
        a = el.find("a", href=True)
        if a:
            href = a["href"]
            data["link"] = f"{BASE_URL}{href}" if href.startswith("/") else href
            # Detect type from URL
            data["type"] = "Show" if "/shows/" in href else "Movie"
            data["slug"] = href.split("/view/")[-1] if "/view/" in href else ""
        else:
            data["link"] = "N/A"; data["type"] = "Movie"; data["slug"] = ""
        yr = el.find("p", class_="year")
        data["year"] = yr.get_text(strip=True) if yr else "N/A"
        rt = el.find("p", class_="rate")
        if rt:
            span = rt.find("span")
            data["rating"] = span.get_text(strip=True) if span else rt.get_text(strip=True)
        else:
            data["rating"] = "N/A"
        qt = el.find("div", class_="quality-tag")
        data["quality"] = qt.get_text(strip=True) if qt else "N/A"
        data["id"] = None  # not available from homepage HTML
        results.append(data)
    return results


def scrape_homepage() -> list[dict]:
    html = fetch_text(BASE_URL)
    return _parse_card_list(html) if html else []


# ═══════════════════════════════════════════════════════════════════════════
#  DISPLAY
# ═══════════════════════════════════════════════════════════════════════════

def _bar(char="─"):
    print(char * _W)


def _print_item(idx: int, item: dict):
    badge = f"[{item.get('quality','?')}]" if item.get('quality') and item['quality'] != 'N/A' else ""
    year   = item.get("year", "?")
    stars  = item.get("rating", "?")
    kind   = item.get("type", "")
    title  = item.get("title", "?")

    title_line = f"  {idx:>2}.  {title}"
    if badge:
        right_pad = _W - len(title_line)
        print(f"{title_line}{badge.rjust(right_pad)}")
    else:
        print(title_line)

    icon = "🎬" if kind == "Movie" else "📺"
    print(f"        {icon} {kind}  |  Year: {year}  |  Rating: {stars}")
    print(f"        {item.get('link','')}")


def print_search_results(result: dict):
    q      = result["query"]
    movies = result["movies"]
    shows  = result["shows"]

    print()
    _bar("═")
    print(f"  Search results for: \"{q}\"")
    _bar("═")

    # build a flat numbered list for selection
    all_items = []

    print(f"\n  MOVIES ({len(movies)})  →  {BASE_URL}/movies/search/?q={urllib.parse.quote_plus(q)}")
    _bar()
    if movies:
        for item in movies:
            idx = len(all_items) + 1
            all_items.append(item)
            _print_item(idx, item)
            print()
    else:
        print("  Nothing Found\n")

    print(f"\n  SHOWS ({len(shows)})  →  {BASE_URL}/shows/search/?q={urllib.parse.quote_plus(q)}")
    _bar()
    if shows:
        for item in shows:
            idx = len(all_items) + 1
            all_items.append(item)
            _print_item(idx, item)
            print()
    else:
        print("  Nothing Found\n")

    _bar("═")
    print(f"  Total: {len(movies)} movie(s)  +  {len(shows)} show(s)")
    _bar("═")

    return all_items  # flat list for selection


def print_homepage_results(items: list[dict]) -> list[dict]:
    all_items = []
    print()
    _bar("═")
    print(f"  LookMovie2 — Homepage  ({len(items)} items)")
    _bar("═")
    for item in items:
        idx = len(all_items) + 1
        all_items.append(item)
        _print_item(idx, item)
        print()
    _bar("═")
    return all_items


def print_play_url(url: str, item: dict, result: dict = None):
    """
    result = {"streams": {quality: m3u8_url}, "subtitles": {lang: [url, ...]}}
    """
    streams   = (result or {}).get("streams", {})
    subtitles = (result or {}).get("subtitles", {})

    print()
    _bar("═")
    print(f"  {item['title']}  ({item.get('year', '')})")
    _bar()
    print(f"  PLAY URL:")
    print(f"  {url}")
    print()

    # ── Streams ──────────────────────────────────────────────────────────
    if streams:
        order = ["1080p", "1080", "720p", "720", "480p", "480", "360p", "360"]
        sorted_streams = sorted(
            streams.items(),
            key=lambda kv: order.index(str(kv[0])) if str(kv[0]) in order else 99
        )
        print(f"  M3U8 STREAMS ({len(sorted_streams)} quality level(s)):")
        _bar()
        for quality, m3u8_url in sorted_streams:
            label = f"{quality}p" if str(quality).isdigit() else quality
            print(f"  [{label:>6}]  {m3u8_url}")
    else:
        print("  M3U8 STREAMS: Not available.")

    print()

    # ── Subtitles ─────────────────────────────────────────────────────────
    if subtitles:
        total_files = sum(len(v) for v in subtitles.values())
        print(f"  SUBTITLES ({len(subtitles)} language(s), {total_files} file(s)):")
        _bar()
        for lang, urls in sorted(subtitles.items()):
            for i, sub_url in enumerate(urls, 1):
                suffix = f" {i}" if len(urls) > 1 else ""
                print(f"  [{lang}{suffix}]  {sub_url}")
    else:
        print("  SUBTITLES: None available.")

    _bar("═")
    print()


# ═══════════════════════════════════════════════════════════════════════════
#  SELECTION PROMPT
# ═══════════════════════════════════════════════════════════════════════════

def prompt_selection(items: list[dict]) -> dict | None:
    if not items:
        return None
    while True:
        try:
            raw = input(f"\n  Select [1-{len(items)}] (or 0 to cancel) > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw == "0" or raw.lower() in ("q", "cancel"):
            return None
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(items):
                return items[n - 1]
        print(f"  Please enter a number between 1 and {len(items)}.")


def prompt_int(label: str, min_val: int = 1, max_val: int = 99) -> int | None:
    while True:
        try:
            raw = input(f"  {label} > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw.isdigit():
            n = int(raw)
            if min_val <= n <= max_val:
                return n
        print(f"  Please enter a number between {min_val} and {max_val}.")


def resolve_and_display(item: dict):
    """
    Resolve the play URL + fetch m3u8 streams, then display everything.
    Handles both movies and shows.
    """
    if item["type"] == "Movie":
        play_url   = get_movie_play_url(item)
        episode_id = None
    else:
        print(f"\n  \"{item['title']}\" is a TV show.")
        season = prompt_int("Enter season number", 1, 99)
        if season is None:
            return
        episode = prompt_int("Enter episode number", 1, 999)
        if episode is None:
            return
        play_url, episode_id = get_show_play_url(item, season, episode)
        if not play_url:
            return

    print(f"  Resolving streams and subtitles ...")
    result = get_streams(play_url, item["type"], episode_id)
    print_play_url(play_url, item, result)


# ═══════════════════════════════════════════════════════════════════════════
#  INTERACTIVE LOOP
# ═══════════════════════════════════════════════════════════════════════════

def interactive_loop():
    print()
    print("  LookMovie2 Search & Play URL Resolver")
    print("  Type a movie / TV show name and press Enter.")
    print("  Commands:  'q' = quit  |  ':home' = homepage listing\n")

    while True:
        try:
            raw = input("  Search > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!")
            break

        if not raw:
            continue
        if raw.lower() in ("q", "quit", "exit"):
            print("  Goodbye!")
            break

        if raw.lower() in (":home", "--home"):
            print("\n  Fetching homepage ...")
            items = scrape_homepage()
            if not items:
                print("  [ERROR] Could not fetch homepage.\n")
                continue
            all_items = print_homepage_results(items)
        else:
            print(f"\n  Searching for \"{raw}\" ...")
            result = search(raw)
            all_items = print_search_results(result)

        if not all_items:
            print("  No results found.\n")
            continue

        # Selection
        selected = prompt_selection(all_items)
        if selected is None:
            print("  Cancelled.\n")
            continue

        # Resolve play URL + m3u8 streams
        resolve_and_display(selected)


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--home" in args:
        print("Fetching homepage ...")
        items = scrape_homepage()
        if items:
            print_homepage_results(items)
        else:
            print("[ERROR] Could not fetch homepage.")

    elif args and args[0] not in ("-h", "--help"):
        # python lookmovies.py "avengers"
        query = " ".join(args)
        print(f"Searching for \"{query}\" ...")
        result = search(query)
        all_items = print_search_results(result)
        if all_items:
            selected = prompt_selection(all_items)
            if selected:
                resolve_and_display(selected)

    else:
        interactive_loop()