import json
import requests
import pandas as pd
from pathlib import Path
from time import sleep
from collections import defaultdict

SPARQL_URL = "https://query.wikidata.org/sparql"

QUERY_TEMPLATE = """
SELECT ?place ?placeLabel ?coord ?image ?countryLabel ?article (COUNT(?sitelink) AS ?sitelinks) WHERE {
  ?place wdt:P31/wdt:P279* wd:%s.
  ?place wdt:P625 ?coord.
  ?place wdt:P18 ?image.

  OPTIONAL { ?place wdt:P17 ?country. }
  OPTIONAL {
    ?article schema:about ?place ;
             schema:isPartOf <https://en.wikipedia.org/>.
  }

  # Count sitelinks as popularity proxy — Taj Mahal ~100, obscure place ~3
  OPTIONAL { ?sitelink schema:about ?place. }

  SERVICE wikibase:label {
    bd:serviceParam wikibase:language "en".
  }
}
GROUP BY ?place ?placeLabel ?coord ?image ?countryLabel ?article
HAVING (COUNT(?sitelink) > 10)
ORDER BY DESC(?sitelinks)
LIMIT %s
OFFSET %s
"""

# Peaks-specific query — avoids expensive hierarchy traversal and COUNT aggregation
PEAKS_QUERY_TEMPLATE = """
SELECT ?place ?placeLabel ?coord ?image ?countryLabel ?article ?sitelinks WHERE {
  ?place wdt:P31 wd:Q8502.
  ?place wdt:P625 ?coord.
  ?place wdt:P18 ?image.
  ?place wikibase:sitelinks ?sitelinks.
  FILTER(?sitelinks > 20)

  OPTIONAL { ?place wdt:P17 ?country. }
  OPTIONAL {
    ?article schema:about ?place ;
             schema:isPartOf <https://en.wikipedia.org/>.
  }

  SERVICE wikibase:label {
    bd:serviceParam wikibase:language "en".
  }
}
ORDER BY DESC(?sitelinks)
LIMIT %s
OFFSET %s
"""

HEADERS = {
    "User-Agent": "GlobalPOIBuilder/1.0 (akshaysodhi422@gmail.com)"
}

# ── Balance config ────────────────────────────────────────────────────────────

MAX_POIS_PER_COUNTRY = 235

CATEGORY_COUNTRY_CAPS = {
    "airports":              5,
    "peaks":                10,
    "volcanoes":             5,
    "deserts":               5,
    "oceans":                0,
    "seas":                  0,
    "rivers":               10,
    "lakes":                10,
    "waterfalls":           10,
    "islands":              10,
    "glaciers":              5,
    "natural":              10,
    "national_parks":       15,
    "cities":               20,
    "landmarks":            30,
    "skyscrapers":          10,
    "bridges":              10,
    "castles":               5,
    "stadiums":              0,
    "universities":         10,
    "museums":               0,
    "infrastructure":        0,
    "space":                 5,
    "markets":               5,
    "history_and_mysteries": 25,
}

DEFAULT_CATEGORY_COUNTRY_CAP = 4

FETCH_LIMIT   = 100
REQUEST_DELAY = 8

# ── Checkpoint config ─────────────────────────────────────────────────────────

CHECKPOINT_DIR  = Path("data/checkpoint")
CHECKPOINT_ROWS = CHECKPOINT_DIR / "rows.csv"
CHECKPOINT_META = CHECKPOINT_DIR / "meta.json"


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def save_checkpoint(all_rows, country_total, country_category, seen_ids, completed_categories):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(all_rows).to_csv(CHECKPOINT_ROWS, index=False)

    meta = {
        "completed_categories": list(completed_categories),
        "seen_ids":             list(seen_ids),
        "country_total":        dict(country_total),
        "country_category":     {k: dict(v) for k, v in country_category.items()},
    }
    with open(CHECKPOINT_META, "w", encoding="utf-8") as f:
        json.dump(meta, f)

    print(f"  [checkpoint saved — {len(all_rows)} rows, "
          f"{len(completed_categories)} categories done]")


def load_checkpoint():
    if not CHECKPOINT_ROWS.exists() or not CHECKPOINT_META.exists():
        return None

    print(f"\nCheckpoint found — resuming from {CHECKPOINT_META}")

    rows_df = pd.read_csv(CHECKPOINT_ROWS)
    all_rows = rows_df.to_dict("records")

    with open(CHECKPOINT_META, "r", encoding="utf-8") as f:
        meta = json.load(f)

    country_total = defaultdict(int, meta["country_total"])

    country_category = defaultdict(lambda: defaultdict(int))
    for country, cats in meta["country_category"].items():
        for cat, n in cats.items():
            country_category[country][cat] = n

    seen_ids             = set(meta["seen_ids"])
    completed_categories = set(meta["completed_categories"])

    print(f"  Rows loaded       : {len(all_rows)}")
    print(f"  Categories done   : {sorted(completed_categories)}")
    print(f"  Unique place IDs  : {len(seen_ids)}")
    print()

    return all_rows, country_total, country_category, seen_ids, completed_categories


def clear_checkpoint():
    for f in [CHECKPOINT_ROWS, CHECKPOINT_META]:
        if f.exists():
            f.unlink()
    print("Checkpoint cleared.")


# ── Wikidata helpers ──────────────────────────────────────────────────────────

def run_query(query: str, retries: int = 6):
    for attempt in range(retries):
        try:
            response = requests.get(
                SPARQL_URL,
                params={"format": "json", "query": query},
                headers=HEADERS,
                timeout=180
            )
            if response.status_code in [429, 500, 502, 503, 504]:
                wait = 5 * (attempt + 1)
                print(f"    Server error {response.status_code}. Retrying in {wait}s...")
                sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            wait = 5 * (attempt + 1)
            print(f"    Request failed: {e}. Retrying in {wait}s...")
            sleep(wait)
    raise Exception("Failed after retries")


def parse_results(data, category):
    rows = []
    for item in data["results"]["bindings"]:
        image_url = item.get("image", {}).get("value")
        if not image_url:
            continue
        country = item.get("countryLabel", {}).get("value") or "Unknown"
        if country.startswith("Q") and country[1:].isdigit():
            country = "Unknown"

        # TEMPORARY: USA only
        if country != "United States":
            continue

        rows.append({
            "wikidata_id":   item.get("place", {}).get("value", "").split("/")[-1],
            "name":          item.get("placeLabel", {}).get("value"),
            "category":      category,
            "coordinates":   item.get("coord", {}).get("value"),
            "image_url":     image_url,
            "country":       country,
            "wikipedia_url": item.get("article", {}).get("value"),
            "sitelinks":     int(item.get("sitelinks", {}).get("value", 0)),
        })
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open("config/categories.json", "r", encoding="utf-8") as f:
        categories = json.load(f)

    with open("config/targets.json", "r", encoding="utf-8") as f:
        targets = json.load(f)

    active_categories = [c for c in categories if targets.get(c, 0) > 0]

    # ── Try to resume from checkpoint ────────────────────────────────────────
    checkpoint = load_checkpoint()
    if checkpoint:
        all_rows, country_total, country_category, seen_ids, completed_categories = checkpoint
    else:
        all_rows             = []
        country_total        = defaultdict(int)
        country_category     = defaultdict(lambda: defaultdict(int))
        seen_ids             = set()
        completed_categories = set()

    print(f"Active categories    : {len(active_categories)}")
    print(f"Max POIs per country : {MAX_POIS_PER_COUNTRY}")
    print()

    # ── Category loop ─────────────────────────────────────────────────────────
    for category in active_categories:

        if category in completed_categories:
            print(f"  Skipping {category} (already completed in checkpoint)")
            continue

        qid          = categories[category]
        target_count = targets[category]

        print(f"\n{'='*60}")
        print(f"  {category.upper()}  |  global target: {target_count}")
        print(f"{'='*60}")

        offset            = 0
        collected         = 0
        empty_page_streak = 0

        try:
            while collected < target_count:
                # Use the lightweight peaks query for peaks category
                if category == "peaks":
                    query = PEAKS_QUERY_TEMPLATE % (FETCH_LIMIT, offset)
                else:
                    query = QUERY_TEMPLATE % (qid, FETCH_LIMIT, offset)

                try:
                    data = run_query(query)
                    rows = parse_results(data, category)
                except Exception as e:
                    print(f"  ERROR at offset {offset}: {e}. Skipping category.")
                    break

                if not rows:
                    empty_page_streak += 1
                    if empty_page_streak >= 2:
                        print(f"  No more results. Stopping at {collected}/{target_count}.")
                        break
                    offset += FETCH_LIMIT
                    sleep(REQUEST_DELAY)
                    continue

                empty_page_streak  = 0
                accepted_this_page = 0

                for row in rows:
                    wid     = row["wikidata_id"]
                    country = row["country"]

                    if wid in seen_ids:
                        continue

                    if country_total[country] >= MAX_POIS_PER_COUNTRY:
                        continue

                    cat_cap = CATEGORY_COUNTRY_CAPS.get(category, DEFAULT_CATEGORY_COUNTRY_CAP)
                    if country_category[country][category] >= cat_cap:
                        continue

                    # ✓ Accept
                    seen_ids.add(wid)
                    country_total[country]              += 1
                    country_category[country][category] += 1
                    all_rows.append(row)
                    collected          += 1
                    accepted_this_page += 1

                    if collected >= target_count:
                        break

                print(f"  offset {offset:>6} | page: {len(rows):>3} | "
                      f"accepted: {accepted_this_page:>3} | "
                      f"category total: {collected}/{target_count}")

                offset += FETCH_LIMIT
                sleep(REQUEST_DELAY)

        except KeyboardInterrupt:
            print(f"\n\nInterrupted during '{category}' at offset {offset}.")
            print(f"Saving checkpoint with {len(all_rows)} rows collected so far...")
            save_checkpoint(all_rows, country_total, country_category,
                            seen_ids, completed_categories)
            print("Run the script again to resume from this point.")
            return

        # Category finished — mark complete and save checkpoint
        completed_categories.add(category)
        save_checkpoint(all_rows, country_total, country_category,
                        seen_ids, completed_categories)

        # Country breakdown for this category
        cat_rows   = [r for r in all_rows if r["category"] == category]
        by_country = defaultdict(int)
        for r in cat_rows:
            by_country[r["country"]] += 1
        top = sorted(by_country.items(), key=lambda x: -x[1])[:10]
        print(f"\n  Top countries for {category}:")
        for c, n in top:
            print(f"    {c:<35} {n:>3}")

    # ── All categories done — write final output ──────────────────────────────
    df = pd.DataFrame(all_rows)
    df = df[df["image_url"].notna()]

    Path("data/raw").mkdir(parents=True, exist_ok=True)
    output = "data/raw/wikidata_pois_images_only.csv"
    df.to_csv(output, index=False)

    clear_checkpoint()

    # ── Final report ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  DONE  |  {len(df):,} total POIs  |  "
          f"{df['country'].nunique()} countries")
    print(f"{'='*60}")

    print(f"\n  Per-category totals:")
    for cat, grp in df.groupby("category"):
        print(f"    {cat:<30} {len(grp):>5}  ({grp['country'].nunique()} countries)")

    print(f"\n  Countries with most POIs (top 20):")
    top_countries = df["country"].value_counts().head(20)
    for country, n in top_countries.items():
        bar = "█" * (n // 2)
        print(f"    {country:<35} {n:>4}  {bar}")

    print(f"\n  Saved: {output}")


if __name__ == "__main__":
    main()
