"""`check --limited`: the read a narrow member-level token can do."""

import io
import json

import pytest
from fakes import FakeCoder, seed_in_sync, small_catalog

ENV = {"CODER_SESSION_TOKEN": "tok", "REQUESTY_API_KEY": "key"}
LIMITED_INFO = "prices and provider base URLs are not checked in limited mode"


@pytest.fixture
def desired(sync):
    return sync.build_desired(small_catalog())


@pytest.fixture
def fake(desired):
    coder = FakeCoder()
    seed_in_sync(coder, desired)
    return coder


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"data": small_catalog()}))
    return str(path)


def run(sync, argv, fake):
    out = io.StringIO()
    code = sync.main(
        argv, ENV, client_factory=lambda base, token: fake, confirm=lambda prompt: "y", out=out
    )
    return code, out.getvalue()


def check(sync, catalog, fake, *flags):
    """Runs `check --json` and returns the exit code and the parsed payload."""
    code, output = run(sync, ["check", "--json", "--catalog-file", catalog, *flags], fake)
    return code, json.loads(output)


def drift(payload):
    return [(f["category"], f["subject"]) for f in payload["findings"] if f["category"] != "INFO"]


def provider_named(fake, name):
    return next(p for p in fake.providers.values() if p["name"] == name)


def model_named(fake, model_id):
    return next(m for m in fake.models.values() if m["model"] == model_id)


def add_provider(fake, name, display_name):
    return fake.create_provider(
        {
            "type": "openai",
            "name": name,
            "display_name": display_name,
            "icon": "https://example.com/icon.png",
            "enabled": True,
            "base_url": "https://example.com/v1",
            "api_keys": ["k"],
        }
    )


def add_model(fake, provider, model_id, **extra):
    return fake.create_model(
        "org-1",
        {
            "ai_provider_id": provider["id"],
            "model": model_id,
            "display_name": model_id,
            "enabled": True,
            "context_limit": 1000,
            **extra,
        },
    )


def test_an_in_sync_coder_has_no_drift(sync, catalog, fake):
    code, output = run(sync, ["check", "--limited", "--catalog-file", catalog], fake)
    assert code == 0
    assert LIMITED_INFO in output
    assert output.strip().endswith("in sync")


def test_limited_never_reads_providers_or_prices(sync, catalog, fake):
    run(sync, ["check", "--limited", "--catalog-file", catalog], fake)
    assert "list_models_response" in fake.reads
    assert "list_providers" not in fake.reads
    assert "list_custom_prices" not in fake.reads
    assert fake.calls == []


def test_full_check_still_reads_providers_and_prices(sync, catalog, fake):
    run(sync, ["check", "--catalog-file", catalog], fake)
    assert "list_providers" in fake.reads
    assert "list_custom_prices" in fake.reads
    assert "list_models_response" not in fake.reads


def test_unrelated_providers_and_models_are_ignored(sync, catalog, fake):
    other = add_provider(fake, "openai", "OpenAI")
    add_model(fake, other, "openai/hand-made")
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 0
    assert drift(payload) == []


def test_a_missing_model_is_reported(sync, catalog, fake):
    model = model_named(fake, "openai/gpt-x")
    del fake.models[model["id"]]
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [("MISSING_MODEL", "openai/gpt-x")]


def test_a_wrong_context_limit_is_model_drift(sync, catalog, fake):
    model_named(fake, "openai/gpt-x")["context_limit"] = 1
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [("MODEL_DRIFT", "openai/gpt-x")]


def test_a_wrong_max_output_tokens_is_model_drift(sync, catalog, fake):
    model_named(fake, "openai/gpt-x")["model_config"] = {"max_output_tokens": 1}
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [("MODEL_DRIFT", "openai/gpt-x")]


def test_a_wrong_icon_is_provider_drift(sync, catalog, fake):
    provider_named(fake, "openai-via-requesty")["icon"] = "https://example.com/wrong.png"
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [("PROVIDER_DRIFT", "openai-via-requesty")]


def test_a_disabled_provider_is_provider_drift(sync, catalog, fake):
    provider_named(fake, "openai-via-requesty")["enabled"] = False
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [("PROVIDER_DRIFT", "openai-via-requesty")]


def test_a_provider_without_a_key_is_provider_drift(sync, catalog, fake):
    provider_named(fake, "openai-via-requesty")["api_keys"] = []
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [("PROVIDER_DRIFT", "openai-via-requesty")]


def test_a_wrong_base_url_is_drift_only_in_full_mode(sync, catalog, fake):
    provider_named(fake, "openai-via-requesty")["base_url"] = "https://example.com/other"
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 0
    assert drift(payload) == []
    code, payload = check(sync, catalog, fake)
    assert code == 1
    assert drift(payload) == [("PROVIDER_DRIFT", "openai-via-requesty")]


def test_a_missing_price_is_drift_only_in_full_mode(sync, catalog, fake):
    fake.prices.clear()
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 0
    assert drift(payload) == []
    code, payload = check(sync, catalog, fake)
    assert code == 1
    assert {category for category, _ in drift(payload)} == {"PRICE_DRIFT"}


def test_a_hand_renamed_provider_looks_missing_with_its_models(sync, catalog, fake):
    provider_named(fake, "openai-via-requesty")["display_name"] = "OpenAI (custom)"
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [
        ("MISSING_PROVIDER", "openai-via-requesty"),
        ("MISSING_MODEL", "openai/gpt-x"),
    ]


def test_a_recognized_provider_with_no_desired_match_yields_orphans(sync, catalog, fake):
    retired = add_provider(fake, "retired-lab-via-requesty", "Retired Lab via Requesty")
    add_model(fake, retired, "retired/model-z")
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 1
    assert drift(payload) == [("ORPHAN_MODEL", "retired/model-z")]
    (orphan,) = [f for f in payload["findings"] if f["category"] == "ORPHAN_MODEL"]
    assert orphan["provider"] == "retired-lab-via-requesty"


def test_a_selected_but_disabled_model_is_info_only(sync, catalog, fake):
    model_named(fake, "openai/gpt-x")["enabled"] = False
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 0
    assert drift(payload) == []
    subjects = [f["subject"] for f in payload["findings"] if f["category"] == "INFO"]
    assert "selected but disabled in Coder: openai/gpt-x" in subjects


def test_json_output_carries_the_limited_finding(sync, catalog, fake):
    code, payload = check(sync, catalog, fake, "--limited")
    assert code == 0
    assert payload["drift"] is False
    assert payload["summary"] == "in sync"
    infos = [f["subject"] for f in payload["findings"] if f["category"] == "INFO"]
    assert infos.count(LIMITED_INFO) == 1


def test_apply_rejects_the_limited_flag(sync, catalog, fake):
    with pytest.raises(SystemExit) as raised:
        run(sync, ["apply", "--limited", "--yes", "--catalog-file", catalog], fake)
    assert raised.value.code == 2
    assert fake.reads == []


class ShapedClient:
    """A client whose models response has the wrong shape."""

    def __init__(self, response):
        self.response = response

    def default_org_id(self):
        return "org-1"

    def list_models_response(self, org_id):
        return self.response


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"models": [], "providers": [{"id": "p1"}]},
        {"models": [{"id": "m1", "ai_provider_id": "p1"}], "providers": [{"id": "p1"}]},
        {"models": [{"id": "m1"}], "providers": [{"id": "p1", "display_name": "A via Requesty"}]},
        {"providers": []},
    ],
)
def test_a_wrong_shaped_response_is_a_sync_error(sync, desired, response):
    with pytest.raises(sync.SyncError, match="unexpected response shape from Coder"):
        sync.load_live_limited(ShapedClient(response), desired)


def test_load_live_limited_names_providers_by_display_name(sync, desired, fake):
    live = sync.load_live_limited(fake, desired)
    assert live.limited is True
    assert live.prices == {}
    assert set(live.providers) == set(desired.providers)
    openai = live.providers["openai-via-requesty"]
    assert openai["display_name"] == "OpenAI via Requesty"
    assert openai["api_keys"]
    assert "base_url" not in openai


def test_list_models_response_returns_the_whole_object(sync, stub):
    body = {"models": [{"id": "m1"}], "providers": [{"id": "p1"}], "unsupported_providers": []}
    stub.responses[("GET", "/api/v2/organizations/org-1/chats/models")] = (200, body)
    client = sync.CoderClient(f"http://127.0.0.1:{stub.server_port}", "tok")
    assert client.list_models_response("org-1") == body
    assert client.list_models("org-1") == [{"id": "m1"}]


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"models": {"id": "m1"}, "providers": []},
        {"models": [], "providers": "none"},
        {"models": []},
    ],
)
def test_list_models_response_rejects_a_wrong_shape(sync, stub, body):
    stub.responses[("GET", "/api/v2/organizations/org-1/chats/models")] = (200, body)
    client = sync.CoderClient(f"http://127.0.0.1:{stub.server_port}", "tok")
    with pytest.raises(sync.ApiError):
        client.list_models_response("org-1")
