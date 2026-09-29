"""Dwell-time policies.

`neutral`   : content-independent. Long dwell with fixed probability, else skip.
              Used by control accounts, and by everyone in baseline and washout.
`treatment` : content-dependent. Long dwell on mental-health-adjacent items,
              skip everything else, and always skip items matching the exclusion list.

Both modes draw exactly the same random numbers per item, so a neutral account's
choices can never depend on what it is shown.
"""
import random
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT


def load_terms(path: Path) -> list[str]:
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            terms.append(re.sub(r"\s+", " ", s.lower()))
    return terms


def compile_terms(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def item_text(meta: dict) -> str:
    parts = [meta.get("title") or "", meta.get("description") or "", " ".join(meta.get("tags") or [])]
    text = " ".join(parts).replace("#", " ").lower()
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Decision:
    long: bool
    dwell_s: float
    reason: str
    matched: str | None = None


class Policy:
    def __init__(self, pcfg: dict, adjacent_re: re.Pattern | None, exclude_re: re.Pattern | None):
        self.skip_lo, self.skip_hi = map(float, pcfg["skip_dwell_s"])
        self.cap = float(pcfg["long_dwell_cap_s"])
        self.jit_lo, self.jit_hi = map(float, pcfg["long_dwell_jitter_s"])
        self.neutral_long_prob = float(pcfg["neutral_long_prob"])
        self.adjacent_re = adjacent_re
        self.exclude_re = exclude_re

    @classmethod
    def from_config(cls, cfg: dict) -> "Policy":
        adj = compile_terms(load_terms(ROOT / cfg["keywords"]["adjacent"]))
        exc = compile_terms(load_terms(ROOT / cfg["keywords"]["exclude"]))
        return cls(cfg["policy"], adj, exc)

    def decide(self, mode: str, meta: dict, rng: random.Random) -> Decision:
        # Fixed number of draws per item, regardless of mode or content.
        u = rng.random()
        skip = rng.uniform(self.skip_lo, self.skip_hi)
        jitter = rng.uniform(self.jit_lo, self.jit_hi)

        duration = meta.get("duration_s") or None
        long_dwell = (min(float(duration), self.cap) if duration else self.cap) + jitter

        if mode == "neutral":
            if u < self.neutral_long_prob:
                return Decision(True, long_dwell, "neutral_long")
            return Decision(False, skip, "neutral_skip")

        if mode == "treatment":
            text = item_text(meta)
            if not text:
                return Decision(False, skip, "no_metadata")
            if self.exclude_re and (m := self.exclude_re.search(text)):
                return Decision(False, skip, "excluded", m.group(0))
            if self.adjacent_re and (m := self.adjacent_re.search(text)):
                return Decision(True, long_dwell, "adjacent_match", m.group(0))
            return Decision(False, skip, "no_match")

        raise ValueError(f"unknown mode: {mode}")
