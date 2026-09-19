import pytest
from fakes import FakeCoder, make_entry, seed_in_sync, small_catalog

ALLOWED_OPS = {
    "create_provider",
    "update_provider",
    "create_model",
    "update_model",
    "upsert_prices",
}


@pytest.fixture
def desired(sync):
    return sync.build_desired(small_catalog())


def plan(sync, desired, fake):
    live = sync.load_live(fake)
    return live, sync.compute_diff(desired, live)


def test_apply_builds_everything_and_is_idempotent(sync, desired):
    fake = FakeCoder()
    live, findings = plan(sync, desired, fake)
    log = []
    sync.apply_changes(fake, live, findings, "secret-key", False, log.append)
    assert len(fake.providers) == 3
    assert len(fake.models) == 3
    assert all(len(p["api_keys"]) == 1 for p in fake.providers.values())
    assert sync.compute_diff(desired, sync.load_live(fake)) == []
    assert not any("secret-key" in line for line in log)


def test_apply_sets_prices_in_micro_dollars(sync, desired):
    fake = FakeCoder()
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "k", False, lambda _: None)
    price = fake.prices[("anthropic", "anthropic/claude-a")]
    assert (price["input_price"], price["output_price"]) == (3_000_000, 15_000_000)
    assert price["cache_read_price"] == 300_000
    assert price["cache_write_price"] is None
    free = fake.prices[("openai", "nvidia/free-y")]
    assert (free["input_price"], free["cache_write_price"]) == (0, 0)


def test_apply_needs_the_key_before_touching_anything(sync, desired):
    fake = FakeCoder()
    live, findings = plan(sync, desired, fake)
    with pytest.raises(sync.SyncError, match="REQUESTY_API_KEY"):
        sync.apply_changes(fake, live, findings, None, False, lambda _: None)
    assert fake.calls == []


def test_apply_without_provider_changes_needs_no_key(sync, desired):
    fake = FakeCoder()
    seed_in_sync(fake, desired)
    del fake.prices[("openai", "openai/gpt-x")]
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, None, False, lambda _: None)
    assert ("openai", "openai/gpt-x") in fake.prices


def test_apply_moves_a_model_to_the_free_provider(sync):
    fake = FakeCoder()
    ids = seed_in_sync(fake, sync.build_desired(small_catalog()))
    catalog = small_catalog()
    catalog[1] = make_entry("openai/gpt-x", "openai", inp=0, out=0, ctx=400_000, maxout=128_000)
    desired = sync.build_desired(catalog)
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "k", False, lambda _: None)
    model = next(m for m in fake.models.values() if m["model"] == "openai/gpt-x")
    assert model["ai_provider_id"] == ids["free-via-requesty"]
    assert fake.prices[("openai", "openai/gpt-x")]["input_price"] == 0


def test_apply_never_deletes(sync, desired):
    fake = FakeCoder()
    ids = seed_in_sync(fake, desired)
    fake.create_model(
        "org-1",
        {"ai_provider_id": ids["openai-via-requesty"], "model": "openai/old", "enabled": True},
    )
    fake.calls.clear()
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "k", False, lambda _: None)
    assert len(fake.models) == 4
    assert {op for op, _ in fake.calls} <= ALLOWED_OPS
    orphan = next(m for m in fake.models.values() if m["model"] == "openai/old")
    assert orphan["enabled"] is False


def test_rotate_key_replaces_every_provider_key(sync, desired):
    fake = FakeCoder()
    seed_in_sync(fake, desired)
    before = {p["id"]: p["api_keys"][0]["id"] for p in fake.providers.values()}
    live, findings = plan(sync, desired, fake)
    sync.apply_changes(fake, live, findings, "new-key", True, lambda _: None)
    after = {p["id"]: p["api_keys"][0]["id"] for p in fake.providers.values()}
    assert all(after[i] != before[i] for i in before)
