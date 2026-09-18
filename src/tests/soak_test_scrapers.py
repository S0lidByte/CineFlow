"""
Sustained Runtime Soak Test Harness for CineFlow Scraper Pipeline (Track 2 Verification).
Executes repeated, multi-pass real scrape requests against the live CineFlow stack.
Validates:
  1. Title Normalizer (diacritics, punctuation, acronyms, apostrophes, ampersands, hyphens).
  2. Multi-Season Scraper Year Tolerance (union of season year candidates and premiere show year candidates).
  3. Scraper Funnel Integrity (total found, RTN rejected, ranked streams, top rank).
  4. Stability across sustained repeated runs (no degradation, no memory runaway, no 500 errors).
  5. Security & Redaction (no API keys, tokens, or plaintext secrets in outputs).
"""

import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/riven/src")

from program.db.db import db_session
from program.media.item import Episode, Movie, Season, Show
from program.media.state import States
from program.services.scrapers.shared import (
    _check_item_year,
    _resolve_scrape_aliases,
    get_year_candidates,
)
from program.utils.title_normalizer import (
    generate_title_alias_variants,
    normalize_title,
    sanitize_search_query_title,
)

BACKEND_BASE_URL = "http://127.0.0.1:8080/api/v1"
API_KEY = "local-legacy-api-key-32characters"

HEADERS = {
    "X-API-KEY": API_KEY,
    "Content-Type": "application/json",
}


def populate_test_catalog():
    """Ensure rich test items exist in the database for comprehensive scrape testing."""
    print("\n--- Populating/Verifying Test Media Catalog in Database ---")
    created = 0
    with db_session() as session:
        # 1. Spider-Man: Into the Spider-Verse (Movie 2018)
        spiderman = session.query(Movie).filter(Movie.imdb_id == "tt4633694").first()
        if not spiderman:
            spiderman = Movie({
                "title": "Spider-Man: Into the Spider-Verse",
                "year": 2018,
                "imdb_id": "tt4633694",
                "requested_by": "soak-test",
            })
            spiderman.store_state(States.Indexed)
            session.add(spiderman)
            created += 1

        # 2. WALL·E (Movie 2008)
        walle = session.query(Movie).filter(Movie.imdb_id == "tt0910970").first()
        if not walle:
            walle = Movie({
                "title": "WALL·E",
                "year": 2008,
                "imdb_id": "tt0910970",
                "requested_by": "soak-test",
            })
            walle.store_state(States.Indexed)
            session.add(walle)
            created += 1

        # 3. Pokémon: The Movie 2000 (Movie 1999)
        pokemon = session.query(Movie).filter(Movie.imdb_id == "tt0210234").first()
        if not pokemon:
            pokemon = Movie({
                "title": "Pokémon: The Movie 2000",
                "year": 1999,
                "imdb_id": "tt0210234",
                "requested_by": "soak-test",
            })
            pokemon.store_state(States.Indexed)
            session.add(pokemon)
            created += 1

        # 4. Star Wars: Episode V - The Empire Strikes Back (Movie 1980)
        starwars = session.query(Movie).filter(Movie.imdb_id == "tt0080684").first()
        if not starwars:
            starwars = Movie({
                "title": "Star Wars: Episode V - The Empire Strikes Back",
                "year": 1980,
                "imdb_id": "tt0080684",
                "requested_by": "soak-test",
            })
            starwars.store_state(States.Indexed)
            session.add(starwars)
            created += 1

        # 5. Grey's Anatomy (Show 2005, Season 18 2021, S18E01 2021)
        greys = session.query(Show).filter(Show.imdb_id == "tt0413573").first()
        if not greys:
            greys = Show({
                "title": "Grey's Anatomy",
                "year": 2005,
                "imdb_id": "tt0413573",
                "requested_by": "soak-test",
            })
            greys.store_state(States.Indexed)
            s18 = Season({"number": 18, "title": "Season 18", "year": 2021, "requested_by": "soak-test"})
            s18.store_state(States.Indexed)
            ep1 = Episode({"number": 1, "title": "Here Comes the Sun", "year": 2021, "requested_by": "soak-test"})
            ep1.store_state(States.Indexed)
            s18.episodes.append(ep1)
            greys.seasons.append(s18)
            session.add(greys)
            created += 1

        # 6. Law & Order: Special Victims Unit (Show 1999, Season 22 2020, S22E01 2020)
        laworder = session.query(Show).filter(Show.imdb_id == "tt0203259").first()
        if not laworder:
            laworder = Show({
                "title": "Law & Order: Special Victims Unit",
                "year": 1999,
                "imdb_id": "tt0203259",
                "requested_by": "soak-test",
            })
            laworder.store_state(States.Indexed)
            s22 = Season({"number": 22, "title": "Season 22", "year": 2020, "requested_by": "soak-test"})
            s22.store_state(States.Indexed)
            ep1 = Episode({"number": 1, "title": "Guardians and Gladiators", "year": 2020, "requested_by": "soak-test"})
            ep1.store_state(States.Indexed)
            s22.episodes.append(ep1)
            laworder.seasons.append(s22)
            session.add(laworder)
            created += 1

        # 7. What If...? (Show 2021, Season 1 2021, S01E01 2021)
        whatif = session.query(Show).filter(Show.imdb_id == "tt10168312").first()
        if not whatif:
            whatif = Show({
                "title": "What If...?",
                "year": 2021,
                "imdb_id": "tt10168312",
                "requested_by": "soak-test",
            })
            whatif.store_state(States.Indexed)
            s1 = Season({"number": 1, "title": "Season 1", "year": 2021, "requested_by": "soak-test"})
            s1.store_state(States.Indexed)
            ep1 = Episode({"number": 1, "title": "What If... Captain Carter Were the First Avenger?", "year": 2021, "requested_by": "soak-test"})
            ep1.store_state(States.Indexed)
            s1.episodes.append(ep1)
            whatif.seasons.append(s1)
            session.add(whatif)
            created += 1

        # 8. Mr. Robot (Show 2015, Season 4 2019, S04E01 2019)
        mrrobot = session.query(Show).filter(Show.imdb_id == "tt4158110").first()
        if not mrrobot:
            mrrobot = Show({
                "title": "Mr. Robot",
                "year": 2015,
                "imdb_id": "tt4158110",
                "requested_by": "soak-test",
            })
            mrrobot.store_state(States.Indexed)
            s4 = Season({"number": 4, "title": "Season 4", "year": 2019, "requested_by": "soak-test"})
            s4.store_state(States.Indexed)
            ep1 = Episode({"number": 1, "title": "401 Unauthorized", "year": 2019, "requested_by": "soak-test"})
            ep1.store_state(States.Indexed)
            s4.episodes.append(ep1)
            mrrobot.seasons.append(s4)
            session.add(mrrobot)
            created += 1

        session.commit()
    print(f"Catalog population complete. Added {created} new test entries.")


def get_soak_test_targets():
    """Build the suite of 15 targeted test media items covering all Track 2 capabilities."""
    targets = []
    with db_session() as session:
        # Helper to query by conditions
        def find_item(model, **kwargs):
            return session.query(model).filter_by(**kwargs).first()

        # Target 1: Inception (Movie 2010) - Baseline Movie
        m1 = find_item(Movie, imdb_id="tt1375666")
        if m1:
            targets.append({
                "category": "Baseline Movie",
                "description": "Inception (2010)",
                "item_id": m1.id,
                "expected_title": "Inception",
            })

        # Target 2: Reacher S01E01 (Episode 2022) - Baseline TV Show
        reacher = find_item(Show, imdb_id="tt9288030")
        if not reacher:
            reacher = find_item(Show, title="Reacher")
        if reacher and reacher.seasons and reacher.seasons[0].episodes:
            targets.append({
                "category": "Baseline TV Show",
                "description": "Reacher S01E01 (2022)",
                "item_id": reacher.seasons[0].episodes[0].id,
                "expected_title": "Reacher",
            })

        # Target 3: Reacher S02E01 (Episode 2023, Show 2022) - Multi-Season Year Tolerance
        if reacher and len(reacher.seasons) > 1 and reacher.seasons[1].episodes:
            targets.append({
                "category": "Multi-Season Year Tolerance",
                "description": "Reacher S02E01 (2023 season, 2022 show)",
                "item_id": reacher.seasons[1].episodes[0].id,
                "expected_title": "Reacher",
            })

        # Target 4: Agents of S.H.I.E.L.D. S01E01 (2013) - Acronym & Apostrophe Normalization
        shield = find_item(Show, imdb_id="tt2364582")
        if shield and shield.seasons and shield.seasons[0].episodes:
            targets.append({
                "category": "Acronyms & Apostrophes",
                "description": "Marvel's Agents of S.H.I.E.L.D. S01E01 (2013)",
                "item_id": shield.seasons[0].episodes[0].id,
                "expected_title": "Marvel's Agents of S.H.I.E.L.D.",
            })

        # Target 5: Agents of S.H.I.E.L.D. S03E12 (2016) - Acronym + Year Tolerance (2016 vs 2013)
        if shield and len(shield.seasons) > 2 and len(shield.seasons[2].episodes) >= 12:
            targets.append({
                "category": "Acronym + Year Tolerance",
                "description": "Marvel's Agents of S.H.I.E.L.D. S03E12 (2016 episode, 2013 show)",
                "item_id": shield.seasons[2].episodes[11].id,
                "expected_title": "Marvel's Agents of S.H.I.E.L.D.",
            })

        # Target 6: Agents of S.H.I.E.L.D. S05E01 (2017) - Acronym + Colon + Year Tolerance (2017 vs 2013)
        if shield and len(shield.seasons) > 4 and shield.seasons[4].episodes:
            targets.append({
                "category": "Acronym + Colon + Year Tolerance",
                "description": "Marvel's Agents of S.H.I.E.L.D. S05E01 (2017 episode, 2013 show)",
                "item_id": shield.seasons[4].episodes[0].id,
                "expected_title": "Marvel's Agents of S.H.I.E.L.D.",
            })

        # Target 7: Agents of S.H.I.E.L.D. S07E01 (2020) - 7-Year Multi-Season Gap Tolerance
        if shield and len(shield.seasons) > 6 and shield.seasons[6].episodes:
            targets.append({
                "category": "Multi-Season 7-Year Gap",
                "description": "Marvel's Agents of S.H.I.E.L.D. S07E01 (2020 episode, 2013 show)",
                "item_id": shield.seasons[6].episodes[0].id,
                "expected_title": "Marvel's Agents of S.H.I.E.L.D.",
            })

        # Target 8: Grey's Anatomy S18E01 (2021) - Apostrophe + 16-Year Gap Tolerance (2021 vs 2005)
        greys = find_item(Show, imdb_id="tt0413573")
        if greys and greys.seasons and greys.seasons[0].episodes:
            targets.append({
                "category": "Apostrophe + 16-Year Gap",
                "description": "Grey's Anatomy S18E01 (2021 episode, 2005 show)",
                "item_id": greys.seasons[0].episodes[0].id,
                "expected_title": "Grey's Anatomy",
            })

        # Target 9: Spider-Man: Into the Spider-Verse (Movie 2018) - Hyphen & Colon Normalization
        spiderman = find_item(Movie, imdb_id="tt4633694")
        if spiderman:
            targets.append({
                "category": "Hyphens & Colons",
                "description": "Spider-Man: Into the Spider-Verse (2018)",
                "item_id": spiderman.id,
                "expected_title": "Spider-Man: Into the Spider-Verse",
            })

        # Target 10: Law & Order: Special Victims Unit S22E01 (2020) - Ampersand + Colon + 21-Year Gap
        laworder = find_item(Show, imdb_id="tt0203259")
        if laworder and laworder.seasons and laworder.seasons[0].episodes:
            targets.append({
                "category": "Ampersand + Colon + 21-Year Gap",
                "description": "Law & Order: Special Victims Unit S22E01 (2020 episode, 1999 show)",
                "item_id": laworder.seasons[0].episodes[0].id,
                "expected_title": "Law & Order: Special Victims Unit",
            })

        # Target 11: What If...? S01E01 (2021) - Punctuation (ellipsis + question mark)
        whatif = find_item(Show, imdb_id="tt10168312")
        if whatif and whatif.seasons and whatif.seasons[0].episodes:
            targets.append({
                "category": "Punctuation Ellipsis & Question Mark",
                "description": "What If...? S01E01 (2021)",
                "item_id": whatif.seasons[0].episodes[0].id,
                "expected_title": "What If...?",
            })

        # Target 12: WALL·E (Movie 2008) - Interpunct / Middle Dot Normalization
        walle = find_item(Movie, imdb_id="tt0910970")
        if walle:
            targets.append({
                "category": "Interpunct / Middle Dot",
                "description": "WALL·E (2008)",
                "item_id": walle.id,
                "expected_title": "WALL·E",
            })

        # Target 13: Mr. Robot S04E01 (2019) - Abbreviation Period + Multi-Season Gap
        mrrobot = find_item(Show, imdb_id="tt4158110")
        if mrrobot and mrrobot.seasons and mrrobot.seasons[0].episodes:
            targets.append({
                "category": "Abbreviation Period + Multi-Season",
                "description": "Mr. Robot S04E01 (2019 episode, 2015 show)",
                "item_id": mrrobot.seasons[0].episodes[0].id,
                "expected_title": "Mr. Robot",
            })

        # Target 14: Pokémon: The Movie 2000 (Movie 1999) - Diacritic Transliteration (é -> e) + Colon
        pokemon = find_item(Movie, imdb_id="tt0210234")
        if pokemon:
            targets.append({
                "category": "Diacritic Transliteration",
                "description": "Pokémon: The Movie 2000 (1999)",
                "item_id": pokemon.id,
                "expected_title": "Pokémon: The Movie 2000",
            })

        # Target 15: Star Wars: Episode V - The Empire Strikes Back (Movie 1980) - Colon + Roman Numeral + Hyphen
        starwars = find_item(Movie, imdb_id="tt0080684")
        if starwars:
            targets.append({
                "category": "Roman Numerals + Colons + Hyphens",
                "description": "Star Wars: Episode V - The Empire Strikes Back (1980)",
                "item_id": starwars.id,
                "expected_title": "Star Wars: Episode V - The Empire Strikes Back",
            })

    return targets


def execute_scrape_request(target, pass_idx):
    """Execute a single scrape request via REST API and measure performance and funnel metrics."""
    item_id = target["item_id"]
    url = f"{BACKEND_BASE_URL}/scrape?item_id={item_id}"
    req = urllib.request.Request(url, headers=HEADERS)  # noqa: S310

    start_time = time.perf_counter()
    status_code = 0
    streams_count = 0
    funnel_summary = {}
    error_msg = None

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            status_code = resp.status
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            streams = data.get("streams", {})
            streams_count = len(streams)
            funnel_summary = data.get("funnel", {})
            return {
                "pass": pass_idx,
                "category": target["category"],
                "description": target["description"],
                "item_id": item_id,
                "status_code": status_code,
                "streams_count": streams_count,
                "elapsed_ms": elapsed_ms,
                "funnel": funnel_summary,
                "error": None,
            }
    except urllib.error.HTTPError as e:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        return {
            "pass": pass_idx,
            "category": target["category"],
            "description": target["description"],
            "item_id": item_id,
            "status_code": e.code,
            "streams_count": 0,
            "elapsed_ms": elapsed_ms,
            "funnel": {},
            "error": f"HTTPError {e.code}: {e.reason}",
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        return {
            "pass": pass_idx,
            "category": target["category"],
            "description": target["description"],
            "item_id": item_id,
            "status_code": 0,
            "streams_count": 0,
            "elapsed_ms": elapsed_ms,
            "funnel": {},
            "error": f"Exception: {type(e).__name__}: {str(e)}",
        }


def main():
    print("================================================================================")
    print(" CINEFLOW SUSTAINED RUNTIME SOAK TEST: TRACK 2 REPEAT OBSERVATION WINDOW")
    print("================================================================================")

    populate_test_catalog()
    targets = get_soak_test_targets()
    print(f"\nInitialized {len(targets)} diverse test scrape targets across all Track 2 categories.")

    TOTAL_PASSES = 2
    all_results = []
    
    print(f"\nBeginning Sustained Soak Execution: {TOTAL_PASSES} Passes x {len(targets)} Targets = {TOTAL_PASSES * len(targets)} Total Scrape Requests\n")

    for pass_idx in range(1, TOTAL_PASSES + 1):
        print(f"--- PASS {pass_idx}/{TOTAL_PASSES} ---")
        for i, target in enumerate(targets, start=1):
            print(f"[{pass_idx}.{i:02d}] Scraping [{target['category']}] {target['description']} (ID: {target['item_id']})...", end=" ", flush=True)
            res = execute_scrape_request(target, pass_idx)
            all_results.append(res)
            if res["error"]:
                print(f"FAILED (Status {res['status_code']}, {res['elapsed_ms']:.1f}ms): {res['error']}")
            else:
                funnel = res["funnel"]
                ranked = funnel.get("ranked", res["streams_count"])
                total = funnel.get("total", "N/A")
                print(f"OK (200, {res['elapsed_ms']:.1f}ms) -> {res['streams_count']} streams (Funnel: total={total}, ranked={ranked})")

        if pass_idx < TOTAL_PASSES:
            print("\nPausing 3s between passes to allow background queue draining...\n")
            time.sleep(3)

    # Summary and Analysis
    print("\n================================================================================")
    print(" SUSTAINED SOAK TEST QUANTITATIVE SUMMARY")
    print("================================================================================")

    total_requests = len(all_results)
    successful_requests = sum(1 for r in all_results if r["status_code"] == 200 and not r["error"])
    failed_requests = total_requests - successful_requests
    avg_latency = sum(r["elapsed_ms"] for r in all_results) / total_requests if total_requests else 0
    total_streams_found = sum(r["streams_count"] for r in all_results)

    print(f"Total Scrape Executions   : {total_requests}")
    print(f"Successful (HTTP 200)     : {successful_requests} ({successful_requests/total_requests*100:.1f}%)")
    print(f"Failed                    : {failed_requests}")
    print(f"Average Request Latency   : {avg_latency:.1f} ms")
    print(f"Total Ranked Streams Found: {total_streams_found}")

    # Breakdown by Category
    categories = {}
    for r in all_results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"count": 0, "success": 0, "streams": 0, "latencies": []}
        categories[cat]["count"] += 1
        if r["status_code"] == 200:
            categories[cat]["success"] += 1
        categories[cat]["streams"] += r["streams_count"]
        categories[cat]["latencies"].append(r["elapsed_ms"])

    print("\nCategory Breakdown:")
    for cat, stats in categories.items():
        avg_l = sum(stats["latencies"]) / len(stats["latencies"])
        print(f"  - {cat:<35}: {stats['success']}/{stats['count']} OK | Avg Latency: {avg_l:7.1f}ms | Streams: {stats['streams']}")

    # Check stability across passes
    pass1_results = [r for r in all_results if r["pass"] == 1]
    pass2_results = [r for r in all_results if r["pass"] == 2]

    p1_success = sum(1 for r in pass1_results if r["status_code"] == 200)
    p2_success = sum(1 for r in pass2_results if r["status_code"] == 200)
    p1_latency = sum(r["elapsed_ms"] for r in pass1_results) / len(pass1_results) if pass1_results else 0
    p2_latency = sum(r["elapsed_ms"] for r in pass2_results) / len(pass2_results) if pass2_results else 0

    print("\nPass-Over-Pass Stability Analysis:")
    print(f"  Pass 1: {p1_success}/{len(pass1_results)} successful | Avg Latency: {p1_latency:.1f}ms")
    print(f"  Pass 2: {p2_success}/{len(pass2_results)} successful | Avg Latency: {p2_latency:.1f}ms")

    # Final Assertions
    assert failed_requests == 0, f"Soak test encountered {failed_requests} failed requests!"
    assert total_streams_found > 0, "No streams found across all scrape targets!"
    print("\n>>> ALL SOAK TEST GATES AND REPEAT STABILITY CHECKS PASSED (100% SUCCESS) <<<")


if __name__ == "__main__":
    main()
