"""Agreement, accuracy, and the small-sample test used by validation and analysis."""
from collections import Counter
from itertools import product

MH = {"adjacent", "mh_distress", "harmful"}      # any sadness / mental-health theme


def cohen_kappa(a: list, b: list) -> float | None:
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)    # undefined when both raters use one category only


def prf(truth: list[bool], pred: list[bool]) -> dict:
    tp = sum(t and p for t, p in zip(truth, pred))
    fp = sum(p and not t for t, p in zip(truth, pred))
    fn = sum(t and not p for t, p in zip(truth, pred))
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec == 0 or rec == 0 else None)
    return {"n": len(truth), "positives": tp + fn, "tp": tp, "fp": fp, "fn": fn,
            "precision": prec, "recall": rec, "f1": f1}


def per_class(truth: list[str], pred: list[str], labels) -> dict:
    return {lab: prf([t == lab for t in truth], [p == lab for p in pred]) for lab in labels}


def confusion(truth: list[str], pred: list[str], labels) -> dict:
    return {t: {p: sum(1 for x, y in zip(truth, pred) if x == t and y == p) for p in labels} for t in labels}


def sign_flip_p(values: list[float]) -> float | None:
    """Exact two-sided sign-flip (randomization) p-value for mean != 0 across independent units.

    With n units the smallest attainable p is 2 / 2**n (e.g. 0.25 for 3 pairs), so with
    the MVP's 2-3 pairs this can only ever be descriptive.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    obs = abs(sum(vals) / len(vals))
    hits = total = 0
    for signs in product((1, -1), repeat=len(vals)):
        total += 1
        hits += abs(sum(s * v for s, v in zip(signs, vals)) / len(vals)) >= obs - 1e-12
    return hits / total
