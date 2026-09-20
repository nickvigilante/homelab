import pytest
from fakes import small_catalog


def client_for(sync, stub):
    return sync.CoderClient(f"http://127.0.0.1:{stub.server_port}", "tok")


def test_default_org_sends_the_token(sync, stub):
    stub.responses[("GET", "/api/v2/organizations")] = (
        200,
        [{"id": "org-1", "is_default": False}, {"id": "org-2", "is_default": True}],
    )
    assert client_for(sync, stub).default_org_id() == "org-2"
    assert stub.requests[0]["headers"]["coder-session-token"] == "tok"


def test_no_default_org_is_an_error(sync, stub):
    stub.responses[("GET", "/api/v2/organizations")] = (200, [{"id": "o", "is_default": False}])
    with pytest.raises(sync.SyncError, match="no default organization"):
        client_for(sync, stub).default_org_id()


def test_list_models_unwraps_the_response(sync, stub):
    stub.responses[("GET", "/api/v2/organizations/org-1/chats/models")] = (
        200,
        {"models": [{"id": "m1"}], "providers": [], "unsupported_providers": []},
    )
    assert client_for(sync, stub).list_models("org-1") == [{"id": "m1"}]


def test_prices_are_listed_from_the_custom_source(sync, stub):
    stub.responses[("GET", "/api/experimental/ai/model-prices")] = (200, [])
    client_for(sync, stub).list_custom_prices()
    assert stub.requests[0]["query"].endswith("?source=custom")


def test_post_sends_json_and_tolerates_no_content(sync, stub):
    stub.responses[("POST", "/api/experimental/ai/model-prices")] = (204, None)
    client_for(sync, stub).upsert_prices([{"provider": "openai", "model": "m"}])
    request = stub.requests[0]
    assert request["headers"]["content-type"] == "application/json"
    assert b'"prices"' in request["body"]


def test_http_errors_become_api_errors(sync, stub):
    stub.responses[("GET", "/api/v2/ai/providers")] = (403, {"message": "forbidden"})
    with pytest.raises(sync.ApiError) as excinfo:
        client_for(sync, stub).list_providers()
    assert excinfo.value.status == 403
    assert "forbidden" in str(excinfo.value)


def test_unreachable_server_becomes_an_api_error(sync, stub):
    client = client_for(sync, stub)
    stub.shutdown()
    stub.server_close()
    with pytest.raises(sync.ApiError) as excinfo:
        client.list_providers()
    assert excinfo.value.status == 0


def test_fetch_catalog_returns_the_data_list(sync, stub):
    stub.responses[("GET", "/v1/models")] = (200, {"object": "list", "data": [{"id": "a/b"}]})
    url = f"http://127.0.0.1:{stub.server_port}/v1/models"
    assert sync.fetch_catalog(url) == [{"id": "a/b"}]


def test_fetch_catalog_rejects_a_malformed_response(sync, stub):
    stub.responses[("GET", "/v1/models")] = (200, {"oops": True})
    with pytest.raises(sync.SyncError, match="no 'data' list"):
        sync.fetch_catalog(f"http://127.0.0.1:{stub.server_port}/v1/models")


def test_a_non_json_success_body_becomes_an_api_error(sync, stub):
    stub.responses[("GET", "/api/v2/ai/providers")] = (200, b"<html>login</html>")
    with pytest.raises(sync.ApiError, match="invalid JSON") as excinfo:
        client_for(sync, stub).list_providers()
    assert excinfo.value.status == 0


def test_a_wrong_shaped_response_becomes_a_sync_error(sync, stub):
    stub.responses[("GET", "/api/v2/organizations")] = (200, [{"id": "org-1", "is_default": True}])
    stub.responses[("GET", "/api/v2/ai/providers")] = (200, [{"message": "nope"}])
    with pytest.raises(sync.SyncError, match="unexpected response shape"):
        sync.load_live(client_for(sync, stub))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("list_providers", "/api/v2/ai/providers"),
        ("list_custom_prices", "/api/experimental/ai/model-prices"),
    ],
)
def test_a_json_object_from_a_list_endpoint_is_an_api_error(sync, stub, method, path):
    stub.responses[("GET", path)] = (200, {})
    with pytest.raises(sync.ApiError, match="expected a JSON list"):
        getattr(client_for(sync, stub), method)()


MODELS_PATH = "/api/v2/organizations/org-1/chats/models"


def test_null_models_and_providers_become_empty_lists(sync, stub):
    stub.responses[("GET", MODELS_PATH)] = (200, {"models": None, "providers": None})
    response = client_for(sync, stub).list_models_response("org-1")
    assert response["models"] == []
    assert response["providers"] == []


def test_a_null_unsupported_providers_becomes_an_empty_list(sync, stub):
    stub.responses[("GET", MODELS_PATH)] = (
        200,
        {"models": [], "providers": None, "unsupported_providers": None},
    )
    response = client_for(sync, stub).list_models_response("org-1")
    assert response == {"models": [], "providers": [], "unsupported_providers": []}
    assert client_for(sync, stub).list_models("org-1") == []


def test_a_non_list_models_value_is_still_rejected(sync, stub):
    stub.responses[("GET", MODELS_PATH)] = (200, {"models": "x", "providers": []})
    with pytest.raises(sync.ApiError, match="expected a JSON object"):
        client_for(sync, stub).list_models_response("org-1")


def test_load_live_and_load_live_limited_accept_null_lists(sync, stub):
    stub.responses[("GET", "/api/v2/organizations")] = (200, [{"id": "org-1", "is_default": True}])
    stub.responses[("GET", "/api/v2/ai/providers")] = (200, [])
    stub.responses[("GET", MODELS_PATH)] = (
        200,
        {"models": None, "providers": None, "unsupported_providers": None},
    )
    stub.responses[("GET", "/api/experimental/ai/model-prices")] = (200, [])
    client = client_for(sync, stub)
    live = sync.load_live(client)
    assert (live.providers, live.models, live.prices) == ({}, [], {})
    limited = sync.load_live_limited(client, sync.build_desired(small_catalog()))
    assert (limited.providers, limited.models, limited.prices) == ({}, [], {})
    assert limited.limited is True
