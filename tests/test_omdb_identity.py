"""Do not substitute loose title matches for the requested film."""

import httpx
import pytest

from nicole import films


@pytest.mark.parametrize(
    "outcome",
    [
        "correct",
        "ambiguous",
        "wrong_year",
        "wrong_detail",
        "wrong_id",
        "missing",
        "auth",
    ],
)
def test_loose_title_match_uses_exact_search_and_verified_imdb_id(monkeypatch, outcome):
    monkeypatch.setenv("OMDB_API_KEY", "test-secret")
    requests = []

    def get(client, url, *, params):
        requests.append(dict(params))
        if "t" in params:
            data = {
                "Response": "True",
                "Title": "Woody Adjacent - Anomalisa (2015)",
                "Year": "2015",
                "imdbID": "tt28424878",
                "Runtime": "N/A",
                "Genre": "N/A",
            }
        elif "s" in params:
            match = {
                "Title": "Anomalisa",
                "Year": "2016",
                "imdbID": "tt2401878",
                "Type": "movie",
            }
            matches = [
                match,
                {**match, "Title": "The Anomalisa Effect", "imdbID": "tt31392467"},
            ]
            if outcome == "ambiguous":
                matches.append({**match, "imdbID": "tt1234567"})
            if outcome == "wrong_year":
                matches[0]["Year"] = "1990"
            data = {"Response": "True", "Search": matches}
            if outcome in {"missing", "auth"}:
                data = {
                    "Response": "False",
                    "Error": "Movie not found!"
                    if outcome == "missing"
                    else "Invalid API key!",
                }
        else:
            assert params["i"] == "tt2401878"
            data = {
                "Response": "True",
                "Title": "Anomalisa" if outcome != "wrong_detail" else "Different Film",
                "Year": "2016",
                "imdbID": "tt2401878" if outcome != "wrong_id" else "tt1234567",
                "Runtime": "90 min",
                "Genre": "Animation, Comedy, Drama",
            }
        return httpx.Response(200, request=httpx.Request("GET", url), json=data)

    monkeypatch.setattr(httpx.Client, "get", get)
    if outcome == "auth":
        with pytest.raises(ValueError, match="API setup"):
            films.lookup("Anomalisa", "2015", True)
    else:
        movie = films.lookup("Anomalisa", "2015", True)
        if outcome == "correct":
            assert movie["title"] == "Anomalisa" and movie["minutes"] == 90
            assert movie["imdb_id"] == "tt2401878"
        else:
            assert movie == {"unavailable": True}
    assert requests[0]["y"] == "2015"
    assert requests[1]["s"] == "Anomalisa" and "y" not in requests[1]
    assert len(requests) == (
        3 if outcome in {"correct", "wrong_detail", "wrong_id"} else 2
    )
