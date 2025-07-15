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
END_TIME = "19:00"
GRAPHQL_URL = "https://www.greenwheels.com/api/graphql"
HEADERS = {
    "Content-Type": "application/json",
    "apollographql-client-name": "web",
    "apollographql-client-version": "6.8.1.0",
    "origin": "https://www.greenwheels.com",
    "referer": "https://www.greenwheels.com/nl/book",
    "user-agent": "Mozilla/5.0",
}

# Functie om streepjes uit kentekens te verwijderen
def clean_license(lic: str) -> str:
    return lic.replace("-", "")

def update_availability(request):
    logger.info("Starting update_availability function.")
    now = datetime.now(tz=tz.gettz("Europe/Amsterdam")).replace(second=0, microsecond=0)
    minute_block = (now.minute // 15) * 15
    start_dt = now.replace(minute=0) + timedelta(minutes=minute_block)
    end_dt = now.replace(hour=19, minute=0, second=0, microsecond=0)

    block_delta = timedelta(minutes=BLOCK_MINUTES)
    blocks = []
    current = start_dt
    while current < end_dt:
        blocks.append((current, current + block_delta))
        current += block_delta

    conflict_dict = {}
    car_metadata = {}
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

    def merge_blocks(blocks, tolerance_minutes=20):
        if not blocks:
            return []
        blocks.sort()
        merged = [blocks[0]]
        for s, e in blocks[1:]:
            last_s, last_e = merged[-1]
            if (s - last_e) <= timedelta(minutes=tolerance_minutes):
                merged[-1] = (last_s, max(last_e, e))
            else:
                merged.append((s, e))
        return merged

    def format_blocks(blocks):
        return ", ".join(f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}" for s, e in blocks)

    rows = []
    for car_id, meta in car_metadata.items():
        conflicts = merge_blocks(conflict_dict.get(car_id, []), tolerance_minutes=20)
        now = datetime.now(tz=tz.gettz("Europe/Amsterdam"))
        end_of_day = now.replace(hour=19, minute=0, second=0, microsecond=0)
        cursor = now
        free_slot_found = False
        for s, e in conflicts:
            if s > cursor:
                free_slot_found = True
                break
            cursor = max(cursor, e)
        no_availability_all_day = not free_slot_found

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
