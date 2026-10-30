"""Read cinema listings. No checkout, reservations, seats, or payment operations."""

import json
import math
import os
from datetime import datetime, time, timedelta
from importlib.resources import files
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

PACIFIC = ZoneInfo("America/Los_Angeles")
# Approximate downtown Oakland search center, not a precise user location.
OAKLAND = {"label": "Oakland · 94612", "latitude": 37.807, "longitude": -122.272}
API = "https://api.internationalshowtimes.com/v5/showtimes"


def ticket_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or any(ord(c) < 32 for c in value):
        return None
    return value


def window(query: dict) -> tuple[datetime, datetime]:
    now = datetime.fromisoformat(query["now"]).astimezone(PACIFIC)
    day = (
        datetime.fromisoformat(query["date"]).date()
        if query.get("date")
        else now.date()
    )
    after = time.fromisoformat(query["after"]) if query.get("after") else time(0)
    start = max(datetime.combine(day, after, PACIFIC), now)
    end = datetime.combine(day + timedelta(days=1), time(0), PACIFIC)
    return start, end


def normalize(data: dict, start: datetime, end: datetime) -> list[dict]:
    movies = {str(m["id"]): m for m in data.get("movies", [])}
    cinemas = {str(c["id"]): c for c in data.get("cinemas", [])}
    found = []
    ids = set()
    for raw in data.get("showtimes", []):
        try:
            when = datetime.fromisoformat(raw["start_at"])
            if when.tzinfo is None or not start <= when < end:
                continue
            movie = movies.get(str(raw.get("movie_id")))
            cinema = cinemas.get(str(raw.get("cinema_id")))
            if not movie or not cinema:
                continue
            title = movie.get("title") or movie.get("original_title")
            if not title or not cinema.get("name") or str(raw["id"]) in ids:
                continue
            ids.add(str(raw["id"]))
            runtime = movie.get("runtime")
            releases = (movie.get("release_dates") or {}).get("US") or []
            year = str(releases[0]["date"])[:4] if releases else "?"
            local = when.astimezone(PACIFIC)
            found.append(
                {
                    "id": str(raw["id"]),
                    "movie_id": str(movie["id"]),
                    "cinema_id": str(cinema["id"]),
                    "cinema": cinema["name"],
                    "starts_at": local.isoformat(),
                    "display_time": local.strftime("%a %b %d, %Y · %-I:%M %p %Z"),
                    "format": ", ".join(raw.get("attribute_codes") or [])
                    or "Unspecified",
                    "ticket_url": ticket_url(raw.get("booking_link"))
                    or ticket_url(raw.get("movie_page_link"))
                    or ticket_url(cinema.get("website")),
                    "movie": {
                        "title": title,
                        "year": year,
                        "movie_id": str(movie["id"]),
                        "imdb_id": movie.get("imdb_id"),
                        "minutes": runtime
                        if isinstance(runtime, int)
                        and not isinstance(runtime, bool)
                        and runtime > 0
                        else None,
                        "genre": ", ".join(
                            g.get("name") or "" for g in (movie.get("genres") or [])
                        )
                        or "Unknown",
                        "plot": movie.get("synopsis") or "",
                    },
                }
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return sorted(found, key=lambda s: s["starts_at"])


def samples(start: datetime, end: datetime, radius_miles: float = 10) -> list[dict]:
    """Reuse captured films, venues, formats, and local times on the demo date."""
    data = json.loads(files("nicole").joinpath("data/cinema_samples.json").read_text())
    cinemas = {cinema["id"]: cinema for cinema in data["cinemas"]}
    distances = {}
    for key, cinema in cinemas.items():
        lat1, lat2 = map(math.radians, (OAKLAND["latitude"], cinema["latitude"]))
        delta = math.radians(cinema["longitude"] - OAKLAND["longitude"])
        angle = math.sin((lat2 - lat1) / 2) ** 2 + (
            math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2) ** 2
        )
        distances[key] = 3958.8 * 2 * math.asin(min(1, math.sqrt(angle)))
    result = []
    for row in data["showtimes"]:
        when = datetime.combine(
            start.date(), time.fromisoformat(row["local_time"]), PACIFIC
        )
        if distances[row["cinema_id"]] > radius_miles or not start <= when < end:
            continue
        result.append(
            {
                "id": f"{row['id']}-{when.isoformat()}",
                "movie_id": row["movie"]["movie_id"],
                "cinema_id": row["cinema_id"],
                "cinema": cinemas[row["cinema_id"]]["name"],
                "starts_at": when.isoformat(),
                "display_time": when.strftime("%a %b %d, %Y · %-I:%M %p %Z"),
                "format": row["format"] + " · Sample screening",
                "sample": True,
                "ticket_url": None,
                "movie": row["movie"],
            }
        )
    return sorted(result, key=lambda showing: showing["starts_at"])


def lookup(query: dict) -> dict:
    backend = query.get("backend", "sample")
    if backend not in {"sample", "international"}:
        raise ValueError("Unsupported screenings source; start a new visit")
    start, end = window(query)
    sample = backend == "sample"
    receipt = {
        "status": "ok",
        "showtimes": [],
        "checked_at": query["now"],
        "display_checked_at": datetime.fromisoformat(query["now"])
        .astimezone(PACIFIC)
        .strftime("%b %d · %-I:%M %p %Z"),
        "source": "sample screenings" if sample else "International Showtimes",
        "location": OAKLAND["label"],
    }
    if start >= end:
        return receipt
    if sample:
        return {
            **receipt,
            "showtimes": samples(start, end, float(query.get("radius_miles", 10))),
        }
    key = os.getenv("SHOWTIMES_API_KEY")
    if not key:
        return {**receipt, "status": "not_configured"}
    params = {
        "location": f"{OAKLAND['latitude']},{OAKLAND['longitude']}",
        "distance": float(query.get("radius_miles", 10)) * 1.609344,
        "countries": "US",
        "time_from": start.isoformat(),
        "time_to": end.isoformat(),
        "append": "movies,cinemas",
        "page_size": 100,
        "fields": "id,movie_id,cinema_id,start_at,attribute_codes,"
        "booking_link,movie_page_link",
        "movie_fields": "id,title,original_title,runtime,genres,synopsis,"
        "imdb_id,release_dates",
        "cinema_fields": "id,name,website",
    }
    results = []
    try:
        with httpx.Client(
            timeout=httpx.Timeout(10, connect=3),
            transport=httpx.HTTPTransport(retries=0),
        ) as client:
            # Bound pagination; report truncation rather than claim exhaustive coverage.
            for _ in range(5):
                response = client.get(API, headers={"X-Api-Key": key}, params=params)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(
                    data.get("showtimes"), list
                ):
                    raise ValueError("Invalid response")
                results.extend(normalize(data, start, end))
                token = (data.get("meta_info") or {}).get("next_page_token")
                if not token:
                    break
                params = {"page_token": token, "page_size": 100}
            else:
                receipt["status"] = "partial"
        unique = {s["id"]: s for s in results}
        return {
            **receipt,
            "showtimes": sorted(unique.values(), key=lambda s: s["starts_at"]),
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        # Never echo API response bodies, URLs, or request headers.
        return {**receipt, "status": "unavailable"}
