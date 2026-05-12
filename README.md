# Global POIs Dataset — Setup & Run Guide

## 1. Clone & enter the project

```bash
git clone <your-repo-url>
cd <project-folder>
```

## 2. Create virtual environment

```bash
python -m venv venv
source venv/bin/activate      # Linux/Mac
# OR
venv\Scripts\activate         # Windows
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Set targets

Edit `config/targets.json` — set a number `> 0` for each category you want to fetch. Categories set to `0` are skipped.

## 5. Clear any stale checkpoint

```bash
rm -f data/checkpoint/meta.json data/checkpoint/rows.csv
```

## 6. Run the pipeline in order

```bash
# Fetch raw POIs from Wikidata
python scripts/extract/wikidata_fetch.py

# Parse coordinates
python scripts/transform/parse_coordinates.py

# Deduplicate
python scripts/transform/deduplicate.py

# Fetch Wikipedia descriptions
python scripts/enrich/fetch_wikipedia_descriptions.py

# Score by popularity
python scripts/enrich/popularity.py

# (Optional) Generate richer descriptions via Gemini
# Requires: export GEMINI_API_KEY=your_key
python scripts/enrich/generate_gemini_descriptions.py

# Export
python scripts/export/export_geojson.py
python scripts/export/geojson_to_gpkg.py    # optional GeoPackage
python scripts/export/load_postgres.py      # optional PostgreSQL
```

## Notes

- The fetch script is **resumable** — if interrupted, re-run it and it picks up from the checkpoint.
- Descriptions fetched via `wikipedia_url` are reliable; name-based fallbacks are worth spot-checking.
- PostgreSQL export requires a local instance running at `localhost:5432` with a `global_pois` database.
