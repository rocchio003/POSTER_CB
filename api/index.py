import json
import os

from flask import Flask, jsonify

app = Flask(__name__)

# I cataloghi sono file JSON nella cartella data/ del repo.
# Li aggiorna lo scraper che gira sul telefono (scraper.py): qui non si scarica nulla da CB01.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CATALOG_FILES = {
    "cb01_omni_movies": "movies.json",
    "cb01_omni_series": "series.json",
    "cb01_latest":      "latest.json",
}


def _find(name: str):
    for root in (BASE_DIR, os.path.dirname(BASE_DIR), os.getcwd()):
        path = os.path.join(root, "data", name)
        if os.path.isfile(path):
            return path
    return None


def load_metas(name: str) -> list:
    path = _find(name)
    if not path:
        print(f"[DATA] file mancante: data/{name}")
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("metas", [])
    except Exception as e:
        print(f"[DATA] errore lettura {name}: {e}")
        return []


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


@app.route("/catalog/<content_type>/<catalog_id>.json")
def catalog(content_type, catalog_id):
    filename = CATALOG_FILES.get(catalog_id)
    if filename is None and content_type == "series":
        filename = "series.json"
    metas = load_metas(filename) if filename else []
    resp = jsonify({"metas": metas})
    resp.headers.add("Access-Control-Allow-Origin", "*")
    if metas:
        resp.headers["Cache-Control"] = "s-maxage=300, stale-while-revalidate=600"
    return resp


@app.route("/debug")
def debug():
    out = {}
    for catalog_id, filename in CATALOG_FILES.items():
        path = _find(filename)
        out[filename] = {
            "trovato": bool(path),
            "titoli":  len(load_metas(filename)) if path else 0,
        }
    resp = jsonify(out)
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
