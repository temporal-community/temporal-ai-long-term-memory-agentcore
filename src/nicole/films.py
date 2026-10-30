"""OMDb is a fact lookup; the model supplies recommendation candidates."""

import os
import re

import httpx

FIXTURES = {
    "Gattaca": {
        "title": "Gattaca",
        "year": "1997",
        "imdb_id": "tt0119177",
        "minutes": 106,
        "genre": "Drama, Sci-Fi, Thriller",
        "plot": "A man challenges a future that believes biology determines destiny.",
    },
    "The Truman Show": {
        "title": "The Truman Show",
        "year": "1998",
        "imdb_id": "tt0120382",
        "minutes": 103,
        "genre": "Comedy, Drama",
        "plot": "An ordinary man questions the extraordinary world around him.",
    },
    "Moon": {
        "title": "Moon",
        "year": "2009",
        "imdb_id": "tt1182345",
        "minutes": 97,
        "genre": "Drama, Mystery, Sci-Fi",
        "plot": "A lunar worker discovers a troubling secret about his assignment.",
    },
}


def parse_movie(data: dict) -> dict:
    if data.get("Response") == "False" and data.get("Error") == "Movie not found!":
        return {"unavailable": True}
    if data.get("Response") != "True":
        # OMDb errors can mention authentication. Never echo provider responses.
        raise ValueError(
            "OMDb could not return this film; check the title and API setup"
        )
    runtime = re.fullmatch(r"(\d+) min", data.get("Runtime", ""))
    return {
        "title": data["Title"],
        "year": data.get("Year", "?"),
        "imdb_id": data["imdbID"],
        "minutes": int(runtime[1]) if runtime else None,
        "genre": data.get("Genre", "Unknown"),
        "plot": data.get("Plot", ""),
    }


def lookup(title: str, year: str | None, live: bool) -> dict:
    if not live:
        if title not in FIXTURES:
            raise ValueError("This film is not in the offline stage catalog")
        return dict(FIXTURES[title])
    key = os.getenv("OMDB_API_KEY")
    if not key:
        raise ValueError("Set OMDB_API_KEY in .env to use live film lookups")
    params = {"apikey": key, "t": title, "type": "movie", "plot": "short"}
    if year:
        params["y"] = str(year)
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=3)) as client:
            response = client.get("https://www.omdbapi.com/", params=params)
            response.raise_for_status()
            movie = parse_movie(response.json())
            if movie.get("unavailable") and year:
                # Release years vary between catalogs. Try the title before giving up.
                params.pop("y")
                response = client.get("https://www.omdbapi.com/", params=params)
                response.raise_for_status()
                movie = parse_movie(response.json())
            if not movie.get("unavailable") and not same_title(movie["title"], title):
                # A title lookup can return a loosely matched, unrelated record.
                # Search without a year filter to allow festival/release differences.
                response = client.get(
                    "https://www.omdbapi.com/",
                    params={"apikey": key, "s": title, "type": "movie"},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("Response") == "False":
                    if data.get("Error") == "Movie not found!":
                        return {"unavailable": True}
                    raise ValueError("OMDb search failed; check API setup")
                if data.get("Response") != "True":
                    raise ValueError("OMDb search returned an invalid response")
                matches = {
                    item["imdbID"]: item
                    for item in data.get("Search", [])
                    if item.get("Type") == "movie"
                    and same_title(item.get("Title", ""), title)
                    and year_matches(item.get("Year", ""), year)
                    and re.fullmatch(r"tt\d+", item.get("imdbID", ""))
                }
                if year:
                    exact = {
                        key: item
                        for key, item in matches.items()
                        if str(item["Year"])[:4] == str(year)
                    }
                    if exact:
                        matches = exact
                if len(matches) != 1:
                    return {"unavailable": True}
                imdb_id = next(iter(matches))
                response = client.get(
                    "https://www.omdbapi.com/",
                    params={
                        "apikey": key,
                        "i": imdb_id,
                        "type": "movie",
                        "plot": "short",
                    },
                )
                response.raise_for_status()
                movie = parse_movie(response.json())
                if not movie.get("unavailable") and movie["imdb_id"] != imdb_id:
                    return {"unavailable": True}
            if not movie.get("unavailable") and (
                not same_title(movie["title"], title)
                or not year_matches(movie["year"], year)
            ):
                return {"unavailable": True}
            return movie
    except (httpx.HTTPError, KeyError, TypeError, httpx.DecodingError):
        # HTTP exception strings contain the URL, including the API key.
        raise RuntimeError(
            "OMDb lookup failed; check connectivity and API setup"
        ) from None


def same_title(actual: str, requested: str) -> bool:
    return " ".join(actual.split()).casefold() == " ".join(requested.split()).casefold()


def year_matches(actual: str, requested: str | None) -> bool:
    if requested is None:
        return True
    actual = str(actual)[:4]
    return (
        actual.isdigit()
        and str(requested).isdigit()
        and abs(int(actual) - int(requested)) <= 1
    )
