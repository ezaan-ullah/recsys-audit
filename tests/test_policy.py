import random

from audit.config import load_config
from audit.metadata import parse_iso_duration
from audit.policy import Policy, compile_terms, item_text

PCFG = {"skip_dwell_s": [2.0, 4.0], "long_dwell_cap_s": 45,
        "long_dwell_jitter_s": [0.0, 2.0], "neutral_long_prob": 0.3}


def make_policy():
    return Policy(PCFG, compile_terms(["sad", "lonely", "mental health"]), compile_terms(["suicide"]))


def meta(title="", tags=None, desc="", duration=30.0):
    return {"title": title, "description": desc, "tags": tags or [], "duration_s": duration}


def test_whole_word_matching():
    rx = compile_terms(["sad"])
    assert rx.search(item_text(meta("so SAD today")))
    assert not rx.search(item_text(meta("sadistic villain")))


def test_hashtags_and_multiword_terms():
    p = make_policy()
    assert p.decide("treatment", meta("night drive #lonely"), random.Random(0)).long
    assert p.decide("treatment", meta(tags=["mental   health"]), random.Random(0)).long


def test_exclusion_beats_adjacent():
    d = make_policy().decide("treatment", meta("sad and suicide"), random.Random(0))
    assert not d.long and d.reason == "excluded" and d.matched == "suicide"


def test_no_metadata_is_skipped():
    d = make_policy().decide("treatment", {"title": None}, random.Random(0))
    assert not d.long and d.reason == "no_metadata"


def test_neutral_mode_ignores_content():
    p = make_policy()
    for seed in range(200):
        a = p.decide("neutral", meta("so sad and lonely"), random.Random(seed))
        b = p.decide("neutral", meta("cooking pasta"), random.Random(seed))
        assert a == b


def test_rng_stream_identical_across_modes():
    # Same number of draws per item in every mode, so later items are unaffected.
    p = make_policy()
    r1, r2 = random.Random(7), random.Random(7)
    p.decide("treatment", meta("sad"), r1)
    p.decide("neutral", meta("pasta"), r2)
    assert r1.random() == r2.random()


def test_long_dwell_capped():
    for seed in range(50):
        d = make_policy().decide("treatment", meta("sad", duration=120), random.Random(seed))
        assert 45 <= d.dwell_s <= 47


def test_neutral_long_rate_matches_config():
    p = make_policy()
    rng = random.Random(1)
    rate = sum(p.decide("neutral", meta(), rng).long for _ in range(20000)) / 20000
    assert abs(rate - 0.3) < 0.02


def test_iso_duration():
    assert parse_iso_duration("PT59S") == 59
    assert parse_iso_duration("PT1M2S") == 62
    assert parse_iso_duration("P0D") is None
    assert parse_iso_duration(None) is None


def test_shipped_config_and_keywords_load():
    p = Policy.from_config(load_config())
    assert p.adjacent_re and p.exclude_re
