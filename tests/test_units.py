import pytest
from scalably_gsc_mcp import server

def test_normalize_filters_builds_group():
    out = server._normalize_filters([{"dimension": "query", "op": "contains", "value": "mcp"}])
    assert out == [{"groupType": "and", "filters": [{"dimension": "query", "operator": "contains", "expression": "mcp"}]}]

def test_normalize_filters_rejects_bad_operator():
    with pytest.raises(ValueError):
        server._normalize_filters([{"dimension": "query", "operator": "like", "expression": "x"}])

def test_validate_hour_requires_hourly_all():
    with pytest.raises(ValueError):
        server._validate_sa_params(["HOUR"], None, None, "final")
    server._validate_sa_params(["HOUR"], "web", "auto", "hourly_all")

def test_redact_hides_bearer_and_private_key():
    text = 'Authorization: Bearer abc.def "private_key": "-----BEGIN"'
    red = server._redact(text)
    assert "abc.def" not in red and "BEGIN" not in red

def test_fail_is_plain_runtime_error():
    with pytest.raises(RuntimeError, match=r"^api_error: nope "):
        server._fail("gsc", "api_error", "nope")

def test_no_private_envelope_in_source():
    import inspect
    src = inspect.getsource(server)
    assert "tool-outcome" + "/v1" not in src and "OUTCOME_SCHEMA" not in src
