import argparse
import json
import random
import shutil

import pandas as pd
import pytest

import analyze
import audit.config as config
import audit.data as data
import audit.policy as policy
import label_items
from audit.htmlpage import md_to_html
from audit.metrics import cohen_kappa, prf, sign_flip_p
from make_label_sample import draw_sample


def test_kappa_and_prf_on_known_inputs():
    assert cohen_kappa(list("aabb"), list("aabb")) == 1.0
    assert cohen_kappa(list("abab"), list("baba")) == -1.0
    assert cohen_kappa([False] * 5, [False] * 5) is None       # no variation: undefined, not perfect
    # 2x2 textbook example: po=0.7, pe=0.5 -> kappa 0.4
    a = [1] * 5 + [0] * 5
    b = [1] * 4 + [0] + [1] * 2 + [0] * 3
    assert cohen_kappa(a, b) == pytest.approx(0.4)
    m = prf([True, True, False, False], [True, False, True, False])
    assert (m["precision"], m["recall"], m["f1"]) == (0.5, 0.5, 0.5)


def test_sign_flip_exact():
    assert sign_flip_p([0.1, 0.2, 0.3]) == pytest.approx(2 / 8)     # all same sign: only the two extremes
    assert sign_flip_p([0.1, -0.1]) == 1.0
    assert sign_flip_p([]) is None


def test_draw_sample_is_stratified_and_deterministic():
    vids = [f"v{i:03d}" for i in range(400)]
    pos = set(vids[:30])
    s = draw_sample(vids, pos, 100, "seed")
    assert len(s) == 100 and len(set(s)) == 100
    assert len(pos & set(s)) == 30            # all positives fit in the half reserved for them
    assert s == draw_sample(vids, pos, 100, "seed")


def test_md_to_html_renders_codebook():
    out = md_to_html((config.ROOT / "docs/codebook.md").read_text(encoding="utf-8"))
    assert "<table>" in out and "<code>mh_distress</code>" in out and "<ul>" in out


def synthetic_rows(effect: float, n_pairs=2, n_items=40, seed=0):
    """Rows for the full protocol; treatment feeds drift by `effect` during treatment."""
    rng = random.Random(seed)
    proto = config.load_protocol(config.load_config())
    rows, labels = [], {}
    k = 0
    for p in range(1, n_pairs + 1):
        for grp in ("treatment", "control"):
            acct = f"{p}{grp[0]}"
            for s in proto["sessions"]:
                if s["phase"] == "seed":
                    continue
                base = 0.10
                rate = base + (effect if (grp == "treatment" and s["phase"] == "treatment") else 0) \
                    + (effect / 2 if (grp == "treatment" and s["phase"] == "washout") else 0)
                for i in range(n_items):
                    k += 1
                    vid = f"x{k:010d}"
                    labels[vid] = "adjacent" if rng.random() < rate else "none"
                    rows.append({"run_id": "r", "account_id": acct, "pair": f"p{p}", "group": grp,
                                 "phase": s["phase"], "session": s["n"], "organic_index": i, "position": i,
                                 "video_id": vid, "title": "t", "tags": [], "is_ad": False, "long": False,
                                 "reason": "neutral_skip", "dwell_s": 4.0, "exposure_s": 4.7})
    return rows, labels


@pytest.fixture
def repo(tmp_path, monkeypatch):
    src = config.ROOT
    for p in ("config.yaml", "protocol.yaml", "accounts.yaml", "keywords", "seeds", "prompts", "docs", "audit"):
        s = src / p
        (shutil.copytree if s.is_dir() else shutil.copy)(s, tmp_path / p)
    for mod in (config, data, policy, label_items, analyze):
        monkeypatch.setattr(mod, "ROOT", tmp_path)
    return tmp_path


def write_dataset(repo, rows, labels, model="m"):
    cfg = config.load_config()
    cfg["labeler"]["model"] = model
    by_slot = {}
    for r in rows:
        by_slot.setdefault((r["phase"], r["session"], r["account_id"]), []).append(r)
    for (phase, n, acct), rs in by_slot.items():
        d = repo / "data/raw" / f"{phase}_s{n:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{acct}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs))
    lp = label_items.labels_path(cfg, False)
    lp.parent.mkdir(parents=True, exist_ok=True)
    key = label_items.labeler_key(cfg)
    lp.write_text("".join(json.dumps({"video_id": v, "key": key, "label": l}) + "\n" for v, l in labels.items()))
    return cfg


def test_estimands_recover_a_planted_effect(repo, monkeypatch):
    rows, labels = synthetic_rows(effect=0.30, n_items=200)
    cfg = write_dataset(repo, rows, labels)
    df, measure = analyze.item_frame(cfg, False)
    assert measure == "gemini"
    acct = analyze.account_sessions(df, measure)
    summ = analyze.pair_summary(analyze.pair_diffs(acct), 0.05)
    assert len(summ) == 2
    assert summ["DiD"].mean() == pytest.approx(0.30, abs=0.06)
    assert summ["persistence"].mean() == pytest.approx(0.15, abs=0.06)
    assert summ["drift_session"].tolist() == [4, 4]           # first treatment session


def test_no_effect_gives_small_did(repo):
    rows, labels = synthetic_rows(effect=0.0, n_items=300, seed=1)
    cfg = write_dataset(repo, rows, labels)
    df, measure = analyze.item_frame(cfg, False)
    summ = analyze.pair_summary(analyze.pair_diffs(analyze.account_sessions(df, measure)), 0.05)
    assert abs(summ["DiD"].mean()) < 0.05


def test_report_renders_end_to_end(repo, monkeypatch):
    rows, labels = synthetic_rows(effect=0.25)
    cfg = write_dataset(repo, rows, labels)
    monkeypatch.setattr(analyze, "load_config", lambda: cfg)
    analyze.main(argparse.Namespace(dry_run=False))
    html = (repo / "reports/mvp_report.html").read_text()
    assert "data:image/png;base64," in html and "difference-in-differences" in html
    assert pd.read_csv(repo / "reports/account_sessions.csv").shape[0] == 2 * 2 * 10
