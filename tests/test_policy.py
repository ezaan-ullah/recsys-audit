import random

import pytest

from audit.config import load_config
from audit.metadata import parse_iso_duration
from audit.policy import Keywords, Policy, compile_terms, item_text

PCFG = {"skip_dwell_s": [3.0, 5.0], "long_dwell_floor_s": 10, "long_dwell_cap_s": 45,
        "long_dwell_jitter_s": [0.0, 2.0], "neutral_long_prob": 0.3, "decision_deadline_s": 2.5,
        "seed_dwell_cap_s": 90}


def meta(title="", tags=None, desc=""):
    return {"title": title, "description": desc, "tags": tags or []}


def kw():
    return Keywords(compile_terms(["sad", "lonely", "mental health", "udaas", "اداس", "💔"]),
                    compile_terms(["suicide", "khud kushi"]))


def test_whole_word_matching():
    rx = compile_terms(["sad"])
    assert rx.search(item_text(meta("so SAD today")))
    assert not rx.search(item_text(meta("sadistic villain")))
    assert not rx.search(item_text(meta("ambassador")))


def test_hashtags_multiword_multilingual_and_emoji():
    k = kw()
    assert k.match(meta("night drive #lonely"))[0] == "lonely"
    assert k.match(meta(tags=["mental   health"]))[0]
    assert k.match(meta("dil ki baat #udaas"))[0] == "udaas"
    assert k.match(meta("آج دل اداس ہے"))[0] == "اداس"
    assert k.match(meta("miss you💔"))[0] == "💔"          # emoji glued to a word still matches
    assert k.match(meta("Khud   Kushi"))[1] == "khud kushi"


def test_exclusion_applies_in_every_mode():
    p = Policy(PCFG)
    for mode, extra in (("treatment", {"adjacent": True}), ("yoked", {"partner_long": True})):
        d = p.decide(mode, p.draw(random.Random(0)), excluded=True, **extra)
        assert not d.long and d.reason == "excluded"
    rng = random.Random(0)
    while True:   # find a draw the neutral schedule wants long, then exclude it
        dr = p.draw(rng)
        if p.wants_long("neutral", dr):
            break
    d = p.decide("neutral", dr, excluded=True)
    assert not d.long and d.reason == "excluded"


def test_neutral_and_yoked_ignore_content():
    p = Policy(PCFG)
    for seed in range(200):
        dr = p.draw(random.Random(seed))
        assert p.decide("neutral", dr, adjacent=True) == p.decide("neutral", dr, adjacent=False)
        assert p.decide("yoked", dr, adjacent=True, partner_long=False) == \
            p.decide("yoked", dr, adjacent=False, partner_long=False)


def test_yoked_follows_partner():
    p = Policy(PCFG)
    dr = p.draw(random.Random(1))
    assert p.decide("yoked", dr, partner_long=True, duration_s=20).long
    assert not p.decide("yoked", dr, partner_long=False).long
    with pytest.raises(ValueError):
        p.decide("yoked", dr)


def test_partners_with_same_seed_get_identical_draws():
    p = Policy(PCFG)
    a, b = random.Random("s|p1|4|1"), random.Random("s|p1|4|1")
    assert [p.draw(a) for _ in range(50)] == [p.draw(b) for _ in range(50)]


def test_long_dwell_clamped_between_floor_and_cap():
    p = Policy(PCFG)
    for seed in range(50):
        dr = p.draw(random.Random(seed))
        assert 45 <= p.long_dwell(120, dr.jitter_s) <= 47
        assert 10 <= p.long_dwell(3, dr.jitter_s) <= 12      # very short Shorts still clearly 'long'
        assert 20 <= p.long_dwell(20, dr.jitter_s) <= 22
        assert 45 <= p.long_dwell(None, dr.jitter_s) <= 47


def test_skip_is_always_longer_than_the_decision_deadline():
    p = Policy(PCFG)
    rng = random.Random(3)
    assert min(p.draw(rng).skip_s for _ in range(5000)) > PCFG["decision_deadline_s"]
    with pytest.raises(ValueError):
        Policy({**PCFG, "skip_dwell_s": [2.0, 4.0]})


def test_neutral_long_rate_matches_config():
    p = Policy(PCFG)
    rng = random.Random(1)
    rate = sum(p.decide("neutral", p.draw(rng)).long for _ in range(20000)) / 20000
    assert abs(rate - 0.3) < 0.02


def test_iso_duration():
    assert parse_iso_duration("PT59S") == 59
    assert parse_iso_duration("PT1M2S") == 62
    assert parse_iso_duration("P0D") is None
    assert parse_iso_duration(None) is None


def test_shipped_config_and_keywords_load():
    cfg = load_config()
    k = Keywords.from_config(cfg)
    assert k.adjacent_re and k.exclude_re
    Policy.from_config(cfg)
    # The emoji most often seen on kindness edits must not trigger.
    assert k.match(meta("A SEA LION BECAME HER HERO ❤️‍🩹"))[0] is None
