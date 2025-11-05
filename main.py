import httpx 
import pandas as pd
from datetime import datetime, timedelta
from dateutil import tz
import json
import os
from flask import make_response
import logging

# ─── Logging configuratie ───
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Firebase projectinstellingen ───
FIREBASE_PROJECT = "gw-availability"
FIREBASE_SITE = FIREBASE_PROJECT
FIREBASE_UPLOAD_PATH = "availability/availability.json"

# ─── GraphQL query ───
GRAPHQL_QUERY = """
fragment CarFields on Car {
  id
  license
  model
  type
  fuelType
  class
  availability { available }
}
fragment LocationFields on CarLocation {
  address
  city { name }
  geoPoint { lat lng }
}
query Locations($period: PeriodInput) {
  locations(period: $period) {
    ...LocationFields
    cars { ...CarFields }
  }
}
"""

# ===== Instellingen =====
BLOCK_MINUTES = 15
GRAPHQL_URL = os.getenv("GRAPHQL_URL")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/graphql-response+json,application/json;q=0.9",
    "apollographql-client-name": "web",
    "apollographql-client-version": "v5.30.2",
    "origin": "https://www.greenwheels.com",
    "referer": "https://www.greenwheels.com/book",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "x-gw-locale": "nl-NL",
}

# Functie om streepjes uit kentekens te verwijderen
def clean_license(lic: str) -> str:
    return lic.replace("-", "")

def update_availability(request):
    logger.info("Starting update_availability function.")
    now = datetime.now(tz=tz.gettz("Europe/Amsterdam")).replace(second=0, microsecond=0)

    # Starttijd afronden op 15-minuten blok
    minute_block = (now.minute // 15) * 15
    start_dt = now.replace(minute=0) + timedelta(minutes=minute_block)

    # Dynamische eindtijd: altijd 10 uur vooruit
    end_dt = now + timedelta(hours=10)

    block_delta = timedelta(minutes=BLOCK_MINUTES)
    blocks = []
    current = start_dt
    while current < end_dt:
        blocks.append((current, current + block_delta))
        current += block_delta

    conflict_dict = {}
    car_metadata = {}
    conflict_blocks_seen = set()

    for start, end in blocks:
        variables = {
            "period": {
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000)
            }
        }
        payload = {
            "operationName": "Locations",
            "query": GRAPHQL_QUERY,
            "variables": variables
        }
        try:
            logger.info(f"Fetching data for time block: {start} to {end}")
            resp = httpx.post(GRAPHQL_URL, headers=HEADERS, json=payload)
            locations = resp.json().get("data", {}).get("locations", [])
            logger.info(f"Fetched {len(locations)} locations")
        except Exception as e:
            logger.error(f"Error fetching locations data: {e}")
            continue
        for loc in locations:
            city = loc.get("city", {}).get("name")
            address = loc.get("address")
            lat = loc.get("geoPoint", {}).get("lat")
            lng = loc.get("geoPoint", {}).get("lng")
            for car in loc.get("cars", []):
                car_id = car["id"]
                available = car.get("availability", {}).get("available", True)
                if car_id not in car_metadata:
                    raw_lic = car.get("license") or ""
                    car_metadata[car_id] = {
                        "license": clean_license(raw_lic),
                        "model": car.get("model"),
                        "type": car.get("type"),
                        "fuelType": car.get("fuelType"),
                        "class": car.get("class"),
                        "city": city,
                        "address": address,
                        "lat": lat,
                        "lng": lng
                    }
                if not available:
                    conflict_dict.setdefault(car_id, []).append((start, end))
                    conflict_blocks_seen.add((start, end))

    # Herhaal ontbrekende blokken
    missing_blocks = set(blocks) - conflict_blocks_seen
    for start, end in missing_blocks:
        logger.warning(f"Blok {start}–{end} mist overal – opnieuw ophalen")
        variables = {
            "period": {
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000)
            }
        }
        payload = {
            "operationName": "Locations",
            "query": GRAPHQL_QUERY,
            "variables": variables
        }
        try:
            resp = httpx.post(GRAPHQL_URL, headers=HEADERS, json=payload)
            locations = resp.json().get("data", {}).get("locations", [])
            for loc in locations:
                for car in loc.get("cars", []):
                    if not car.get("availability", {}).get("available", True):
                        conflict_dict.setdefault(car["id"], []).append((start, end))
        except Exception as e:
            logger.error(f"Herhaalverzoek voor blok {start}–{end} mislukt: {e}")

    def merge_blocks(blocks):
        if not blocks:
            return []
        blocks.sort()
        merged = [blocks[0]]
        for s, e in blocks[1:]:
            last_s, last_e = merged[-1]
            if s <= last_e:
                merged[-1] = (last_s, max(last_e, e))
            else:
                merged.append((s, e))
        return merged

    def format_blocks(blocks):
        return ", ".join(f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}" for s, e in blocks)

    rows = []
    for car_id, meta in car_metadata.items():
        conflicts = merge_blocks(conflict_dict.get(car_id, []))

        # Bepaal of er een vrije periode van minimaal 30 minuten is binnen het 10u-venster
        free_period_found = False
        current_time = now
        for s, e in conflicts:
            if s > current_time:
                free_duration = s - current_time
                if free_duration >= timedelta(minutes=30):
                    free_period_found = True
                    break
            current_time = max(current_time, e)
        if current_time < end_dt:
            remaining_time = end_dt - current_time
            if remaining_time >= timedelta(minutes=30):
                free_period_found = True

        no_availability_all_day = not free_period_found

        # Corrigeer marges alleen als auto niet volledig bezet is
        if not no_availability_all_day:
            corrected_conflicts = []
            for s, e in conflicts:
                corrected_start = s
                corrected_end = e
                if e < end_dt:
                    corrected_end = e - timedelta(minutes=15)
                if s > start_dt:
                    corrected_start = s + timedelta(minutes=15)
                if corrected_end > corrected_start:
                    corrected_conflicts.append((corrected_start, corrected_end))
            conflicts = corrected_conflicts

        rows.append({
            "license": meta["license"],
            "no_availability_all_day": no_availability_all_day,
            "conflict_tijden": format_blocks(conflicts)
        })

    availability = {
        r["license"]: {
            "no_availability_all_day": r["no_availability_all_day"],
            "conflict_tijden": r["conflict_tijden"]
        } for r in rows
    }

    output_path = os.path.join("availability", "availability.json")
    os.makedirs("availability", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(availability, f, ensure_ascii=False, indent=2)

    logger.info(f"availability.json opgeslagen in: {output_path}")
    logger.info("Availability update completed successfully.")

if __name__ == "__main__":
    update_availability(None)
