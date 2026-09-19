import pytest


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
