import json
import pathlib

import pytest
from fakes import make_entry

SNAPSHOT = pathlib.Path(__file__).parent / "fixtures" / "catalog_snapshot.json"
LOGOS = "https://www.requesty.ai/provider_logos/v2/"
FALLBACK = "https://www.requesty.ai/Requesty_logo.svg"


@pytest.fixture
def snap(sync):
    return sync.build_desired(json.loads(SNAPSHOT.read_text())["data"])


def by_display_name(desired, name):
    return [m for m in desired.models.values() if m.display_name == name]


# ---- real-catalog snapshot -------------------------------------------------


def test_snapshot_picks_the_first_party_entry(snap):
    model = snap.models["anthropic/claude-sonnet-4-5"]
    assert model.provider == "anthropic-via-requesty"
    assert model.provider_type == "anthropic"
    assert model.context_limit == 1_000_000
    assert model.max_output_tokens == 64_000
    assert model.prices == (3_000_000, 15_000_000, 300_000, None)
    assert "bedrock/claude-sonnet-4-5" not in snap.models


def test_snapshot_ignores_regions_and_service_tiers(snap):
    gpt5 = by_display_name(snap, "gpt-5")
    assert [m.model for m in gpt5] == ["openai/gpt-5"]
    assert gpt5[0].provider == "openai-via-requesty"
    assert gpt5[0].provider_type == "openai"
    assert gpt5[0].prices[:2] == (1_250_000, 10_000_000)


def test_snapshot_merges_the_moonshotai_alias(snap):
    kimi = by_display_name(snap, "kimi-k2.6")
    assert [m.model for m in kimi] == ["moonshot/kimi-k2.6"]
    assert kimi[0].provider == "moonshot-via-requesty"


def test_snapshot_collapses_a_model_spanning_labs(snap):
    glm = by_display_name(snap, "glm-5.2")
    assert [m.model for m in glm] == ["zai/glm-5.2"]
    assert glm[0].provider == "zai-via-requesty"
    assert "collapsed glm-5.2: kept lab zai, dropped lab deepinfra" in snap.info


def test_snapshot_gives_free_models_their_own_provider(snap):
    free = snap.providers["free-via-requesty"]
    assert free.type == "openai"
    assert free.display_name == "Free models via Requesty"
    assert free.icon == FALLBACK
    for model_id in (
        "google/gemma-4-31b-it",
        "nvidia/nemotron-3-nano-30b-a3b",
        "mistral/leanstral-1-5",
    ):
        assert snap.models[model_id].provider == "free-via-requesty"
        assert snap.models[model_id].prices == (0, 0, 0, 0)


def test_snapshot_keeps_the_paid_twin_of_a_free_model(snap):
    assert snap.models["deepinfra/google/gemma-4-31B-it"].provider == "google-via-requesty"
    assert snap.providers["google-via-requesty"].type == "google"
    twin = snap.models["deepinfra/nvidia/Nemotron-3-Nano-30B-A3B"]
    assert twin.provider == "nvidia-via-requesty"


def test_snapshot_skips_free_models_without_tool_calling(snap):
    assert "poolside/laguna-m.1" not in snap.models
    assert "skipped (free, no tool calling): poolside/laguna-m.1" in snap.info


# ---- host preference -------------------------------------------------------


def test_first_party_plain_entry_beats_cheaper_hosts(sync):
    desired = sync.build_desired(
        [
            make_entry("bedrock/foo", inp=1e-6, out=1e-6),
            make_entry("acme/foo", inp=2e-6, out=2e-6),
            make_entry("acme/foo:flex", inp=1e-7, out=1e-7),
        ]
    )
    assert list(desired.models) == ["acme/foo"]


def test_cheapest_plain_entry_when_no_first_party(sync):
    desired = sync.build_desired(
        [
            make_entry("a/foo", inp=3e-6, out=0),
            make_entry("b/foo@eu", inp=1e-6, out=0),
            make_entry("c/foo", inp=2e-6, out=0),
        ]
    )
    assert list(desired.models) == ["c/foo"]


def test_cheapest_overall_when_nothing_is_plain(sync):
    desired = sync.build_desired(
        [make_entry("x/foo@eu", inp=5e-6, out=0), make_entry("y/foo@us", inp=2e-6, out=0)]
    )
    assert list(desired.models) == ["y/foo@us"]


def test_cross_lab_tie_goes_to_the_alphabetically_first_lab(sync):
    desired = sync.build_desired(
        [
            make_entry("zeta/x", lab="zeta", canonical="x"),
            make_entry("alpha/x", lab="alpha", canonical="x"),
        ]
    )
    assert list(desired.models) == ["alpha/x"]
    assert desired.models["alpha/x"].provider == "alpha-via-requesty"


def test_qwen_is_an_alias_of_alibaba(sync):
    desired = sync.build_desired([make_entry("alibaba/qwen3", lab="qwen")])
    assert desired.models["alibaba/qwen3"].provider == "alibaba-via-requesty"


# ---- eligibility -----------------------------------------------------------


def test_entries_without_tool_calling_or_prices_or_context_are_ineligible(sync):
    entries = [
        make_entry("a/no-tools", tools=False),
        make_entry("a/no-price", inp=None),
        make_entry("a/no-context", ctx=0),
        make_entry("a/ok"),
    ]
    assert list(sync.build_desired(entries).models) == ["a/ok"]


def test_a_missing_price_is_not_free(sync):
    assert sync.is_free(make_entry("a/z", inp=0, out=0))
    assert not sync.is_free(make_entry("a/z", inp=None, out=0))
    assert not sync.is_free(make_entry("a/z", inp=0, out=1e-6))


def test_truncated_catalog_is_refused(sync, monkeypatch):
    monkeypatch.setattr(sync, "MIN_ELIGIBLE_MODELS", 100)
    with pytest.raises(sync.SyncError, match="only 1 eligible"):
        sync.build_desired([make_entry("a/ok")])


# ---- retirement ------------------------------------------------------------


OCT_16 = 1_792_108_800  # 2026-10-16T00:00:00Z


def test_retiring_entries_are_skipped_and_reported(sync):
    desired = sync.build_desired([make_entry("a/keep"), make_entry("a/going", retires=OCT_16)])
    assert list(desired.models) == ["a/keep"]
    assert "skipped (retires 2026-10-16): going" in desired.info


def test_another_host_keeps_a_retiring_model_registered(sync):
    desired = sync.build_desired(
        [
            make_entry("acme/foo", canonical="foo", retires=OCT_16),
            make_entry("other/foo", canonical="foo"),
        ]
    )
    assert list(desired.models) == ["other/foo"]
    assert not any("retires" in line for line in desired.info)


def test_the_earliest_retirement_date_is_reported(sync):
    desired = sync.build_desired(
        [
            make_entry("a/old", canonical="old", retires=OCT_16),
            make_entry("b/old", canonical="old", retires=OCT_16 - 86_400),
            make_entry("a/keep"),
        ]
    )
    assert "skipped (retires 2026-10-15): old" in desired.info


def test_a_retiring_free_model_is_skipped_too(sync):
    desired = sync.build_desired(
        [make_entry("a/free", inp=0, out=0, retires=OCT_16), make_entry("a/keep")]
    )
    assert "free-via-requesty" not in desired.providers


# ---- providers, prices, icons ----------------------------------------------


@pytest.mark.parametrize(
    ("lab", "expected"),
    [
        ("anthropic", LOGOS + "anthropic.png"),
        ("moonshot", LOGOS + "moonshot.png"),
        ("minimax", LOGOS + "minimaxi.png"),
        ("gryphe", FALLBACK),
    ],
)
def test_icon_for(sync, lab, expected):
    assert sync.icon_for(lab) == expected


@pytest.mark.parametrize(
    ("lab", "provider_type"),
    [("anthropic", "anthropic"), ("google", "google"), ("mistral", "openai")],
)
def test_provider_type(sync, lab, provider_type):
    assert sync.lab_provider(lab).type == provider_type


def test_provider_naming(sync):
    provider = sync.lab_provider("thinkingmachines")
    assert provider.name == "thinkingmachines-via-requesty"
    assert provider.display_name == "Thinking Machines via Requesty"
    assert sync.lab_provider("brand-new").display_name == "Brand-New via Requesty"


def test_price_conversion(sync):
    assert sync.micro(1.25e-6) == 1_250_000
    assert sync.micro(3.3000000000000003e-06) == 3_300_000
    assert sync.micro(0) == 0
    assert sync.micro(None) is None


def test_zero_max_output_tokens_means_unknown(sync):
    desired = sync.build_desired([make_entry("a/x", maxout=0)])
    assert desired.models["a/x"].max_output_tokens is None


def test_a_non_plain_fallback_is_reported(sync):
    desired = sync.build_desired(
        [make_entry("acme/foo:flex", canonical="foo"), make_entry("acme/bar")]
    )
    assert set(desired.models) == {"acme/foo:flex", "acme/bar"}
    assert "only a non-plain entry is available: acme/foo:flex" in desired.info
    assert not any("acme/bar" in line for line in desired.info)
