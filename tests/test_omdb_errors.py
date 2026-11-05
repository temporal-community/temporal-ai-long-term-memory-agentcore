"""OMDb failures name the cause, and a rejected key is not retried."""

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from nicole import activities, films


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [(401, "auth_error", False), (403, "auth_error", False), (503, "http_error", True)],
)
def test_http_status_reaches_the_activity_failure(
    monkeypatch, status, category, retryable
):
    monkeypatch.setenv("OMDB_API_KEY", "test-secret")

    def get(client, url, *, params):
        return httpx.Response(
            status, request=httpx.Request("GET", url, params=params), json={}
        )

    monkeypatch.setattr(httpx.Client, "get", get)
    with pytest.raises(films.LookupFailure) as lookup:
        films.lookup("Arrival", "2016", True)
    assert (lookup.value.category, lookup.value.status) == (category, status)
    assert "test-secret" not in str(lookup.value)

    with pytest.raises(ApplicationError) as failure:
        activities.safe_call(films.lookup, "Arrival", "2016", True)
    assert f"({category}, HTTP {status})" in str(failure.value)
    assert failure.value.non_retryable is not retryable
    assert "test-secret" not in str(failure.value)


def test_connection_failure_is_retryable_and_names_the_cause(monkeypatch):
    monkeypatch.setenv("OMDB_API_KEY", "test-secret")

    def get(client, url, *, params):
        raise httpx.ConnectError("failed: https://www.omdbapi.com/?apikey=test-secret")

    monkeypatch.setattr(httpx.Client, "get", get)
    with pytest.raises(ApplicationError) as failure:
        activities.safe_call(films.lookup, "Arrival", "2016", True)
    assert "(connection_error)" in str(failure.value)
    assert not failure.value.non_retryable
    assert "test-secret" not in str(failure.value)
