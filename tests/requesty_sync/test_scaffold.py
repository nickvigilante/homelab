def test_module_constants(sync):
    assert sync.FREE_PROVIDER_NAME == "free-via-requesty"
    assert sync.REQUESTY_BASE_URL == "https://router.requesty.ai/v1"
    assert (sync.EXIT_OK, sync.EXIT_DRIFT, sync.EXIT_ERROR) == (0, 1, 2)


def test_api_error_carries_status(sync):
    error = sync.ApiError("GET", "/x", 500, "boom")
    assert error.status == 500
    assert "GET /x failed (500): boom" in str(error)
