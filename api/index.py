import re
import time
import threading
from flask import Flask, jsonify, request as freq
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# ─── CONFIGURA QUI ─────────────────────────────────────────────────────────────
OMDB_API_KEY = "34621f55"     # https://www.omdbapi.com/apikey.aspx
TMDB_API_KEY = "b6a0ccf54e2f808390e4626b0e98ebd8"     # https://www.themoviedb.org/settings/api

# Aggiorna quando CB01 cambia dominio
CB01_BASE_URL   = "https://cb01uno.bond"
CB01_SERIES_URL = "https://cb01uno.bond/serietv/"
# ──────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

_imdb_cache: dict = {}
_cache_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# PULIZIA TITOLO
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_unicode(text: str) -> str:
    for orig, repl in {
        '\u2019': "'", '\u2018': "'", '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
    }.items():
        text = text.replace(orig, repl)
    return text


def _extract_year(raw: str) -> str | None:
    m = re.search(r'\b(19|20)\d{2}\b', raw)
    return m.group(0) if m else None


def _clean_title(raw: str) -> str:
    title = _normalize_unicode(raw.strip())
    title = re.sub(r'\s*[\(\[]\d{4}[\)\]]', '', title)
    title = re.sub(r'\s+\d{4}\s*$', '', title)
    title = re.sub(r'\s*\[.*?\]', '', title)
    title = re.sub(r'\s*\((?!\d{4}).*?\)', '', title)
    title = re.sub(r'\s*[-–:]\s*(stagione|season|s\d{1,2}(\s*e\d+)?|ep\.?\s*\d+).*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s+(stagione|season)\s+\d+.*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\b(ita|eng|hd|4k|bluray|bdrip|dvdrip|webrip|web-dl|sub|dubbed)\b.*', '', title, flags=re.IGNORECASE)
    parts = re.split(r'\s+[-–]\s+', title)
    if len(parts) > 1 and len(parts[0]) >= 4:
        title = parts[0]
    return title.strip(" .-–")


def _remove_article(title: str) -> str:
    return re.sub(
        r'^(il|la|lo|gli|le|i|l\'|the|a|an|un|una|uno)\s+',
        '', title, flags=re.IGNORECASE
    ).strip()


def _strip_trailing_number(title: str) -> str | None:
    m = re.match(r'^(.+?)\s+\d{1,2}$', title)
    return m.group(1).strip() if m else None


# ─────────────────────────────────────────────────────────────────────────────
# OMDB
# ─────────────────────────────────────────────────────────────────────────────

def _omdb_exact(title: str, omdb_type: str, year: str | None = None) -> str | None:
    params = {"apikey": OMDB_API_KEY, "t": title, "type": omdb_type}
    if year:
        params["y"] = year
    try:
        data = requests.get("https://www.omdbapi.com/", params=params, timeout=5).json()
        if data.get("Response") == "True":
            return data.get("imdbID")
    except Exception as e:
        print(f"[OMDB exact] '{title}': {e}")
    time.sleep(0.08)
    return None


def _omdb_search(title: str, omdb_type: str, year: str | None = None) -> tuple:
    params = {"apikey": OMDB_API_KEY, "s": title, "type": omdb_type}
    if year:
        params["y"] = year
    try:
        data = requests.get("https://www.omdbapi.com/", params=params, timeout=5).json()
        if data.get("Response") == "True" and data.get("Search"):
            first = data["Search"][0]
            return first.get("imdbID"), first.get("Title", "")
    except Exception as e:
        print(f"[OMDB search] '{title}': {e}")
    time.sleep(0.08)
    return None, ""


# ─────────────────────────────────────────────────────────────────────────────
# TMDB
# ─────────────────────────────────────────────────────────────────────────────

def _tmdb_search(title: str, media_type: str, year: str | None = None) -> str | None:
    if TMDB_API_KEY == "LA_TUA_KEY_TMDB":
        return None
    tmdb_type = "tv" if media_type == "series" else "movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "it-IT", "include_adult": "false"}
    if year:
        params["year" if tmdb_type == "movie" else "first_air_date_year"] = year
    try:
        results = requests.get(
            f"https://api.themoviedb.org/3/search/{tmdb_type}",
            params=params, timeout=5
        ).json().get("results", [])
        if not results:
            return None
        tmdb_id = results[0].get("id")
        if not tmdb_id:
            return None
        ext = requests.get(
            f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/external_ids",
            params={"api_key": TMDB_API_KEY}, timeout=5
        ).json()
        return ext.get("imdb_id") or None
    except Exception as e:
        print(f"[TMDB search] '{title}': {e}")
    time.sleep(0.08)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# RICERCA IMDB — cascata OMDB (7 livelli) + TMDB (4 livelli)
# ─────────────────────────────────────────────────────────────────────────────

def get_imdb_id(raw_title: str, media_type: str = "movie") -> str | None:
    omdb_type = "series" if media_type == "series" else "movie"
    clean     = _clean_title(raw_title)
    year      = _extract_year(raw_title)
    cache_key = f"{media_type}:{clean.lower()}"

    with _cache_lock:
        if cache_key in _imdb_cache:
            return _imdb_cache[cache_key]

    imdb_id = None

    # OMDB — 7 livelli
    if OMDB_API_KEY != "LA_TUA_KEY_OMDB":
        imdb_id = _omdb_exact(clean, omdb_type)
        if not imdb_id and year:
            imdb_id = _omdb_exact(clean, omdb_type, year)
        if not imdb_id:
            no_art = _remove_article(clean)
            if no_art != clean:
                imdb_id = _omdb_exact(no_art, omdb_type, year)
        if not imdb_id and year:
            imdb_id, _ = _omdb_search(clean, omdb_type, year)
        if not imdb_id:
            imdb_id, _ = _omdb_search(clean, omdb_type)
        if not imdb_id:
            words = clean.split()
            if len(words) > 3:
                imdb_id, _ = _omdb_search(" ".join(words[:3]), omdb_type, year)
        if not imdb_id:
            no_art = _remove_article(clean)
            if no_art != clean:
                imdb_id, _ = _omdb_search(no_art, omdb_type, year)

    # TMDB — 4 livelli fallback
    if not imdb_id:
        imdb_id = _tmdb_search(clean, media_type, year)
    if not imdb_id and year:
        imdb_id = _tmdb_search(clean, media_type)
    if not imdb_id:
        no_art = _remove_article(clean)
        if no_art != clean:
            imdb_id = _tmdb_search(no_art, media_type, year)
    if not imdb_id:
        no_num = _strip_trailing_number(clean)
        if no_num:
            imdb_id = _tmdb_search(no_num, media_type, year)

    with _cache_lock:
        _imdb_cache[cache_key] = imdb_id
    return imdb_id


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING CB01
# ─────────────────────────────────────────────────────────────────────────────

def scrape_content(url: str, is_serie: bool = False) -> list:
    content_list = []
    media_type   = "series" if is_serie else "movie"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return content_list
        soup  = BeautifulSoup(response.text, "html.parser")
        posts = soup.select("div.card.mp-post.horizontal")
        if not posts:
            posts = soup.select("div.post, div.ext-post, article, .ci-item")
        for post in posts:
            title_el = post.select_one("h3.card-title a[href]")
            if not title_el:
                title_el = post.find("h3") or post.find("h2") or post.find("a")
            img_el = post.select_one(".card-image img[src]")
            if not img_el:
                img_el = post.find("img")
            if not title_el or not img_el:
                continue
            raw_title  = title_el.get_text(strip=True)
            page_url   = title_el.get("href", "") if title_el.name == "a" else ""
            if not page_url and title_el.find("a"):
                page_url = title_el.find("a").get("href", "")
            poster_url = img_el.get("src", "")
            if not raw_title or len(raw_title) < 3:
                continue
            if any(x in raw_title.lower() for x in ("banner", "cb01", "logo")):
                continue
            if not poster_url or not page_url:
                continue
            if poster_url.startswith("/"):  poster_url = f"{CB01_BASE_URL}{poster_url}"
            if page_url.startswith("/"):    page_url   = f"{CB01_BASE_URL}{page_url}"
            poster_url = poster_url.replace("http://", "https://")
            imdb_id    = get_imdb_id(raw_title, media_type)
            content_id = imdb_id if imdb_id else (
                ("cb01s" if is_serie else "cb01m") + page_url.encode("utf-8").hex()
            )
            meta_item = {
                "id":          content_id,
                "type":        media_type,
                "name":        _clean_title(raw_title),
                "poster":      poster_url,
                "description": f"Novità da CB01.\nPagina originale: {page_url}"
            }
            if is_serie:
                meta_item["genres"] = ["Serie TV"]
            content_list.append(meta_item)
    except Exception as e:
        print(f"[CB01] Errore scraping {url}: {e}")
    return content_list


def scrape_latest() -> list:
    items = []
    try:
        response = requests.get(CB01_BASE_URL, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return items
        soup    = BeautifulSoup(response.text, "html.parser")
        entries = []
        for widget_id in ["rpwe_widget-2", "rpwe_widget-3", "rpwe_widget-4", "rpwe_widget-1"]:
            entries = soup.select(f"#{widget_id} ul.rpwe-ul li.rpwe-li")
            if entries:
                break
        if not entries:
            entries = soup.select("ul.rpwe-ul li.rpwe-li")
        for entry in entries:
            title_el   = entry.select_one("h3.rpwe-title a[href]") or entry.find("a")
            img_el     = entry.select_one("img.rpwe-thumb") or entry.find("img")
            if not title_el or not img_el:
                continue
            raw_title  = title_el.get_text(strip=True)
            page_url   = title_el.get("href", "")
            poster_url = re.sub(r'-\d+x\d+(?=\.\w{3,4}$)', '', img_el.get("src", ""))
            if not raw_title or len(raw_title) < 3:
                continue
            if any(x in raw_title.lower() for x in ("banner", "cb01", "logo")):
                continue
            if not poster_url or not page_url:
                continue
            if poster_url.startswith("/"):  poster_url = f"{CB01_BASE_URL}{poster_url}"
            if page_url.startswith("/"):    page_url   = f"{CB01_BASE_URL}{page_url}"
            poster_url = poster_url.replace("http://", "https://")
            is_serie   = "/serietv/" in page_url
            media_type = "series" if is_serie else "movie"
            imdb_id    = get_imdb_id(raw_title, media_type)
            content_id = imdb_id if imdb_id else (
                ("cb01s" if is_serie else "cb01m") + page_url.encode("utf-8").hex()
            )
            items.append({
                "id":          content_id,
                "type":        media_type,
                "name":        _clean_title(raw_title),
                "poster":      poster_url,
                "description": f"Ultimo aggiunto su CB01.\nPagina originale: {page_url}"
            })
    except Exception as e:
        print(f"[CB01-latest] Errore: {e}")
    return items


def deduplicate(items: list) -> list:
    seen, unique = set(), []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/manifest.json")
def manifest():
    data = {
        "id":          "org.cb01omnivixcataloguerealimdbv37",
        "version":     "37.0.0",
        "name":        "CB01 Cataloghi per OmniVix",
        "description": "Locandine aggiornate da CB01. Riproduzione tramite ID IMDb reali (OMDB+TMDB).",
        "resources":   ["catalog"],
        "types":       ["movie", "series"],
        "catalogs": [
            {"type": "movie",  "id": "cb01_omni_movies", "name": "CB01 – Ultimi Film"},
            {"type": "series", "id": "cb01_omni_series", "name": "CB01 – Ultime Serie TV"},
            {"type": "movie",  "id": "cb01_latest",      "name": "CB01 – Ultimi Aggiunti"},
        ],
        "idPrefixes": ["tt", "cb01m", "cb01s"],
    }
    resp = jsonify(data)
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# CATALOG
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/catalog/<content_type>/<catalog_id>.json")
def catalog(content_type, catalog_id):
    if catalog_id == "cb01_omni_series" or content_type == "series":
        metas = deduplicate(scrape_content(CB01_SERIES_URL, is_serie=True))
    elif catalog_id == "cb01_latest":
        metas = deduplicate(scrape_latest())
    else:
        metas = deduplicate(scrape_content(CB01_BASE_URL, is_serie=False))
    resp = jsonify({"metas": metas})
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
