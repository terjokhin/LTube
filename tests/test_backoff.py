import pytest

from ltube.backoff import BackoffConfig, parse_range


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5-15", (5.0, 15.0)), ("10", (10.0, 10.0)), ("0.5-2", (0.5, 2.0))],
)
def test_parse_range(value, expected):
    assert parse_range(value) == expected


@pytest.mark.parametrize("value", ["15-5", "abc", "-3", "1-x"])
def test_parse_range_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_range(value)


def test_retry_delay_is_exponential_with_equal_jitter():
    cfg = BackoffConfig(base=10, cap=600)
    for attempt in range(12):
        nominal = min(600, 10 * 2**attempt)
        for _ in range(50):
            assert nominal / 2 <= cfg.retry_delay(attempt) <= nominal


def test_track_pause_stays_in_range():
    cfg = BackoffConfig(sleep_min=2, sleep_max=3)
    assert all(2 <= cfg.track_pause() <= 3 for _ in range(100))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sleep_min": 5, "sleep_max": 1},
        {"base": 0},
        {"base": 100, "cap": 10},
        {"request_sleep": -1},
        {"retries": -1},
        {"max_consecutive_failures": 0},
        {"rate_limit": 0},
    ],
)
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        BackoffConfig(**kwargs)
