from collections import Counter

import pytest
from fakes import FakeCoder, make_entry, seed_in_sync, small_catalog


@pytest.fixture
def desired(sync):
    return sync.build_desired(small_catalog())


@pytest.fixture
def fake(desired):
    coder = FakeCoder()
    seed_in_sync(coder, desired)
    return coder


def diff(sync, desired, fake):
    return sync.compute_diff(desired, sync.load_live(fake))


def only(findings, category):
    return [f for f in findings if f.category == category]


def test_a_fresh_coder_reports_everything_missing(sync, desired):
    findings = diff(sync, desired, FakeCoder())
    counts = Counter(f.category for f in findings)
    assert counts[sync.MISSING_PROVIDER] == 3
    assert counts[sync.MISSING_MODEL] == 3
    assert counts[sync.PRICE_DRIFT] == 3


def test_a_synced_coder_has_no_findings(sync, desired, fake):
    assert diff(sync, desired, fake) == []


def test_provider_icon_drift(sync, desired, fake):
    provider = next(p for p in fake.providers.values() if p["name"] == "openai-via-requesty")
    provider["icon"] = "https://example.com/wrong.png"
    (finding,) = diff(sync, desired, fake)
    assert finding.category == sync.PROVIDER_DRIFT
    assert finding.action["payload"] == {"icon": desired.providers["openai-via-requesty"].icon}


def test_disabled_provider_and_missing_key(sync, desired, fake):
    provider = next(p for p in fake.providers.values() if p["name"] == "openai-via-requesty")
    provider["enabled"] = False
    provider["api_keys"] = []
    findings = only(diff(sync, desired, fake), sync.PROVIDER_DRIFT)
    assert {f.action["op"] for f in findings} == {"update_provider", "set_key"}


def test_model_context_drift(sync, desired, fake):
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    model["context_limit"] = 1
    (finding,) = diff(sync, desired, fake)
    assert finding.category == sync.MODEL_DRIFT
    assert finding.action["payload"] == {"context_limit": 400_000}
    assert finding.action["move_to"] is None


def test_max_output_tokens_drift_keeps_operator_tuning(sync, desired, fake):
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    model["model_config"] = {"temperature": 0.2, "max_output_tokens": 1}
    (finding,) = diff(sync, desired, fake)
    assert finding.action["payload"]["model_config"] == {
        "temperature": 0.2,
        "max_output_tokens": 128_000,
    }


def test_enabled_and_display_name_are_not_drift(sync, desired, fake):
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    model["enabled"] = False
    model["display_name"] = "My favourite"
    assert diff(sync, desired, fake) == []


def test_a_model_that_becomes_free_moves_to_the_free_provider(sync, fake):
    catalog = small_catalog()
    catalog[1] = make_entry("openai/gpt-x", "openai", inp=0, out=0, ctx=400_000, maxout=128_000)
    desired = sync.build_desired(catalog)
    findings = diff(sync, desired, fake)
    (moved,) = only(findings, sync.MODEL_DRIFT)
    assert moved.action["move_to"] == "free-via-requesty"
    assert only(findings, sync.PRICE_DRIFT)[0].action["price"]["input_price"] == 0


def test_price_drift(sync, desired, fake):
    del fake.prices[("openai", "openai/gpt-x")]
    fake.prices[("anthropic", "anthropic/claude-a")]["output_price"] = 1
    absent, changed = only(diff(sync, desired, fake), sync.PRICE_DRIFT)
    assert changed.detail == "absent" or absent.detail == "absent"
    assert {absent.detail == "absent", changed.detail == "absent"} == {True, False}


def test_enabled_orphans_are_reported_and_disabled_ones_are_not(sync, desired, fake):
    provider_id = next(
        p["id"] for p in fake.providers.values() if p["name"] == "openai-via-requesty"
    )
    fake.create_model(
        "org-1", {"ai_provider_id": provider_id, "model": "openai/old", "enabled": True}
    )
    fake.create_model(
        "org-1", {"ai_provider_id": provider_id, "model": "openai/older", "enabled": False}
    )
    (orphan,) = only(diff(sync, desired, fake), sync.ORPHAN_MODEL)
    assert orphan.subject == "openai/old"
    assert orphan.action["op"] == "disable_model"


def test_unmanaged_providers_and_models_are_invisible(sync, desired, fake):
    other = fake.create_provider(
        {
            "name": "openai",
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_keys": ["k"],
        }
    )
    fake.create_model("org-1", {"ai_provider_id": other["id"], "model": "gpt-5", "enabled": True})
    assert diff(sync, desired, fake) == []


def test_info_lines_are_not_drift(sync):
    desired = sync.build_desired(
        small_catalog() + [make_entry("poolside/lag", "poolside", inp=0, out=0, tools=False)]
    )
    coder = FakeCoder()
    seed_in_sync(coder, desired)
    findings = diff(sync, desired, coder)
    assert [f.category for f in findings] == [sync.INFO]
    assert not sync.has_drift(findings)
    assert sync.summarize(findings) == "in sync"


def test_summary_counts_and_flags_new_free_models(sync, desired):
    findings = diff(sync, desired, FakeCoder())
    assert sync.summarize(findings) == (
        "drift: 3 missing provider, 3 missing model, 3 price drift; 1 new free model"
    )


def test_report_groups_findings_by_category(sync, desired):
    findings = diff(sync, desired, FakeCoder())
    report = sync.format_report(findings, sync.summarize(findings))
    assert "MISSING_PROVIDER (3)" in report
    assert "  openai/gpt-x: create under openai-via-requesty" in report
    assert report.endswith(sync.summarize(findings))


def test_summary_flags_a_paid_model_that_becomes_free(sync, fake):
    catalog = small_catalog()
    catalog[1] = make_entry("openai/gpt-x", "openai", inp=0, out=0, ctx=400_000, maxout=128_000)
    desired = sync.build_desired(catalog)
    summary = sync.summarize(diff(sync, desired, fake))
    assert summary == "drift: 1 model drift, 1 price drift; 1 new free model"
