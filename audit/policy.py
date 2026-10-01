"""Dwell-time policies.

`neutral`  : content-blind. Long dwell with a fixed, pre-registered probability.
             Used by both arms in baseline and washout.
`treatment`: long dwell exactly on mental-health-adjacent items.
`yoked`    : the control arm during treatment. Long dwell exactly where its treatment
             partner lingered at the same organic position, whatever this item is.

In every mode an excluded item (harmful by keyword or classifier) is never long-dwelled,
so the safety rule is identical for both arms.

Draws come from a stream shared by the two accounts of a pair, a fixed three draws per
organic item, so partners get identical skip lengths and identical neutral schedules.
"""
import random
import re
from dataclasses import dataclass

from .config import ROOT, load_terms

MODES = ("neutral", "treatment", "yoked")


def compile_terms(terms: list[str]) -> re.Pattern | None:
    """Whole-word match for word-like terms; plain substring match for emoji and symbols."""
    if not terms:
        return None
    alts = []
    for t in sorted(set(terms), key=len, reverse=True):
        pre = r"(?<!\w)" if re.match(r"\w", t) else ""
        post = r"(?!\w)" if re.search(r"\w$", t) else ""
        alts.append(pre + r"\s+".join(re.escape(w) for w in t.split(" ")) + post)
    return re.compile("|".join(alts), re.IGNORECASE)


def item_text(meta: dict) -> str:
    parts = [meta.get("title") or "", meta.get("description") or "", " ".join(meta.get("tags") or [])]
    text = " ".join(parts).replace("#", " ").lower()
    return re.sub(r"\s+", " ", text).strip()


class Keywords:
    """Multilingual keyword lists: the trigger's fallback and part of the exclusion rule."""

    def __init__(self, adjacent_re: re.Pattern | None, exclude_re: re.Pattern | None):
        self.adjacent_re = adjacent_re
        self.exclude_re = exclude_re

    @classmethod
    def from_config(cls, cfg: dict) -> "Keywords":
        return cls(compile_terms(load_terms(ROOT / cfg["keywords"]["adjacent"])),
                   compile_terms(load_terms(ROOT / cfg["keywords"]["exclude"])))

    def match(self, meta: dict) -> tuple[str | None, str | None]:
        """(first adjacent term, first exclude term) found in the item's text."""
        text = item_text(meta)
        adj = self.adjacent_re.search(text) if (text and self.adjacent_re) else None
        exc = self.exclude_re.search(text) if (text and self.exclude_re) else None
        return (adj.group(0) if adj else None), (exc.group(0) if exc else None)


@dataclass(frozen=True)
class Draw:
    u: float
    skip_s: float
    jitter_s: float


@dataclass(frozen=True)
class Decision:
    long: bool
    dwell_s: float
    reason: str


class Policy:
    def __init__(self, pcfg: dict):
        self.skip_lo, self.skip_hi = map(float, pcfg["skip_dwell_s"])
        self.floor = float(pcfg["long_dwell_floor_s"])
        self.cap = float(pcfg["long_dwell_cap_s"])
        self.jit_lo, self.jit_hi = map(float, pcfg["long_dwell_jitter_s"])
        self.neutral_long_prob = float(pcfg["neutral_long_prob"])
        self.deadline = float(pcfg["decision_deadline_s"])
        self.seed_cap = float(pcfg["seed_dwell_cap_s"])
        if self.skip_lo <= self.deadline:
            raise ValueError("policy.skip_dwell_s must start above decision_deadline_s, "
                             "or skip lengths would depend on classifier latency")

    @classmethod
    def from_config(cls, cfg: dict) -> "Policy":
        return cls(cfg["policy"])

    def draw(self, rng: random.Random) -> Draw:
        # Exactly three draws per organic item, in every mode.
        return Draw(rng.random(), rng.uniform(self.skip_lo, self.skip_hi), rng.uniform(self.jit_lo, self.jit_hi))

    def ad_skip(self, rng: random.Random) -> float:
        # Ads use a separate per-account stream: partners see ads at different points.
        return rng.uniform(self.skip_lo, self.skip_hi)

    def long_dwell(self, duration_s: float | None, jitter_s: float) -> float:
        base = self.cap if not duration_s else min(max(float(duration_s), self.floor), self.cap)
        return base + jitter_s

    def wants_long(self, mode: str, draw: Draw, partner_long: bool | None = None) -> bool | None:
        """Schedule before content is checked. None means 'depends on whether the item is adjacent'."""
        if mode == "neutral":
            return draw.u < self.neutral_long_prob
        if mode == "yoked":
            if partner_long is None:
                raise ValueError("yoked mode needs the partner's decision")
            return partner_long
        if mode == "treatment":
            return None
        raise ValueError(f"unknown mode: {mode}")

    def decide(self, mode: str, draw: Draw, *, adjacent: bool = False, excluded: bool = False,
               duration_s: float | None = None, partner_long: bool | None = None) -> Decision:
        want = self.wants_long(mode, draw, partner_long)
        if want is None:
            want = adjacent
        if want and excluded:
            return Decision(False, draw.skip_s, "excluded")
        if want:
            return Decision(True, self.long_dwell(duration_s, draw.jitter_s), f"{mode}_long")
        return Decision(False, draw.skip_s, f"{mode}_skip")

    def seed_dwell(self, duration_s: float | None, draw: Draw) -> float:
        base = self.seed_cap if not duration_s else min(max(float(duration_s), self.floor), self.seed_cap)
        return base + draw.jitter_s
