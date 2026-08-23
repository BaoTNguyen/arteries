"""Caller-supplied usage: the escape hatch for CLIs whose transcripts we can't read."""

from arteries.usage import reported_usage


def test_reads_declared_counts():
    assert reported_usage({
        "ARTERIES_USAGE_TOKENS_IN": "1200",
        "ARTERIES_USAGE_TOKENS_OUT": "340",
        "ARTERIES_USAGE_CACHE_READ": "50",
    }) == {"tokens_in": 1200, "tokens_out": 340, "cache_read": 50}


def test_drops_junk_rather_than_coercing():
    # a garbled count is worse than none — it prices as if it were measured
    assert reported_usage({
        "ARTERIES_USAGE_TOKENS_IN": "not-a-number",
        "ARTERIES_USAGE_TOKENS_OUT": "-5",
        "ARTERIES_USAGE_CACHE_READ": "0",
        "ARTERIES_USAGE_CACHE_WRITE_5M": " 12 ",
    }) == {"cache_write_5m": 12}


def test_empty_when_nothing_declared():
    assert reported_usage({}) == {}
    assert reported_usage({"ARTERIES_USAGE_BOGUS": "9"}) == {}
