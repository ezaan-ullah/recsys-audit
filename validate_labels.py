"""Inter-rater agreement, the adjudicated gold set, and the accuracy of every instrument.

    python validate_labels.py            # after both labelers saved their files in data/labels/human/
    python validate_labels.py --dry-run  # instruments evaluated on pilot data

Steps:
  1. Cohen's kappa between the two human labelers (4-class, and binary any-MH / harmful).
  2. Disagreements go to data/labels/adjudication.csv. Fill the `gold` column together,
     then re-run: the gold set = agreements + adjudicated items.
  3. Against the gold set: Gemini outcome labels (the measurement), the live trigger,
     and the keyword lists. Results -> data/labels/validation.json (used by analyze.py).
"""
import argparse
import csv
import json
import sys

from audit.config import ROOT, load_config
from audit.data import data_root, item_meta, latest_labels, load_meta_cache, load_rows, read_jsonl
from audit.metrics import MH, cohen_kappa, confusion, per_class, prf
from audit.policy import Keywords
from label_items import LABELS, labeler_key, labels_path


def load_humans(human_dir) -> dict[str, dict[str, dict]]:
    out = {}
    for f in sorted(human_dir.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        out[d["labeler"]] = {v: l for v, l in d["labels"].items() if l.get("label") in LABELS}
    return out


def read_adjudication(path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["video_id"]: r["gold"].strip() for r in csv.DictReader(f) if r.get("gold", "").strip() in LABELS}


def agreement(a: dict, b: dict) -> dict:
    both = sorted(set(a) & set(b))
    la, lb = [a[v]["label"] for v in both], [b[v]["label"] for v in both]
    return {
        "n_both": len(both),
        "percent_agreement": sum(x == y for x, y in zip(la, lb)) / len(both) if both else None,
        "kappa_4class": cohen_kappa(la, lb),
        "kappa_any_mh": cohen_kappa([x in MH for x in la], [y in MH for y in lb]),
        "kappa_harmful": cohen_kappa([x == "harmful" for x in la], [y == "harmful" for y in lb]),
    }


def evaluate(name: str, gold: dict[str, str], pred: dict[str, str], pos_any: set, pos_harm: set,
             four_class: bool = False) -> dict:
    vids = sorted(v for v in gold if v in pred)
    truth, p = [gold[v] for v in vids], [pred[v] for v in vids]
    out = {"instrument": name, "n": len(vids),
           "any_mh": prf([t in MH for t in truth], [x in pos_any for x in p]),
           "harmful": prf([t == "harmful" for t in truth], [x in pos_harm for x in p])}
    if four_class:
        p4 = ["harmful" if x == "blocked" else x for x in p]
        out["accuracy"] = sum(t == x for t, x in zip(truth, p4)) / len(vids) if vids else None
        out["kappa_vs_gold"] = cohen_kappa(truth, p4)
        out["per_class"] = per_class(truth, p4, LABELS)
        out["confusion"] = confusion(truth, p4, LABELS)
    return out


def fmt(x):
    return "  -  " if x is None else f"{x:.2f}"


def main(args) -> None:
    cfg = load_config()
    labels_dir = ROOT / cfg["paths"]["labels_dir"]
    sample = json.loads((labels_dir / "sample.json").read_text(encoding="utf-8"))
    humans = load_humans(labels_dir / "human")
    if not humans:
        sys.exit(f"No labeler files in {labels_dir / 'human'}.")
    names = sorted(humans)
    result = {"sample_id": sample["sample_id"], "labelers": names}
    rows = load_rows(data_root(cfg, args.dry_run))

    # 1-2. agreement and gold
    adj_path = labels_dir / "adjudication.csv"
    adjudicated = read_adjudication(adj_path)
    gold, disagreements = {}, []
    if len(names) >= 2:
        a, b = humans[names[0]], humans[names[1]]
        result["agreement"] = agreement(a, b)
        for v in sorted(set(a) & set(b)):
            if a[v]["label"] == b[v]["label"]:
                gold[v] = a[v]["label"]
            elif v in adjudicated:
                gold[v] = adjudicated[v]
            else:
                disagreements.append((v, a[v]["label"], b[v]["label"]))
        titles = {r["video_id"]: r.get("title") for r in rows}
        with open(adj_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["video_id", "title", names[0], names[1], "gold"])
            for v in sorted(set(a) & set(b)):
                if a[v]["label"] != b[v]["label"]:
                    w.writerow([v, titles.get(v, ""), a[v]["label"], b[v]["label"], adjudicated.get(v, "")])
    else:
        print("Only one labeler: no agreement statistics; that labeler's labels are the gold set.")
        gold = {v: l["label"] for v, l in humans[names[0]].items()}
    result["gold_n"] = len(gold)
    result["unadjudicated"] = len(disagreements)
    result["gold_distribution"] = {lab: sum(g == lab for g in gold.values()) for lab in LABELS}

    # 3. instruments
    rows_by_vid = {}
    for r in rows:
        rows_by_vid.setdefault(r["video_id"], r)
    gem_key = labeler_key(cfg) if cfg["labeler"].get("model") else None
    gem = {v: r["label"] for v, r in latest_labels(labels_path(cfg, args.dry_run), gem_key).items()}
    trig = {r["video_id"]: r["label"] for r in read_jsonl(ROOT / cfg["paths"]["trigger_cache"])}
    for r in rows:      # keyword-decided live verdicts are not in the cache; use the row's model label if any
        if r.get("trigger_source") in ("gemini", "cache") and r["video_id"] not in trig:
            trig[r["video_id"]] = r["trigger_label"]
    kw = Keywords.from_config(cfg)
    meta_cache = load_meta_cache(cfg)
    kw_pred = {}
    for v in gold:
        adj, exc = kw.match(item_meta(v, rows_by_vid, meta_cache))
        kw_pred[v] = "harmful" if exc else ("adjacent" if adj else "none")

    result["instruments"] = [
        evaluate("gemini_outcome", gold, gem, MH | {"blocked"}, {"harmful", "blocked"}, four_class=True),
        evaluate("live_trigger", gold, trig, {"adjacent", "harmful", "blocked"}, {"harmful", "blocked"}),
        evaluate("keywords", gold, kw_pred, {"adjacent", "harmful"}, {"harmful"}),
    ]
    (labels_dir / "validation.json").write_text(json.dumps(result, indent=1), encoding="utf-8")

    if "agreement" in result:
        ag = result["agreement"]
        print(f"Labelers {names[0]} vs {names[1]}: n={ag['n_both']}  agreement={fmt(ag['percent_agreement'])}  "
              f"kappa 4-class={fmt(ag['kappa_4class'])}  any-MH={fmt(ag['kappa_any_mh'])}  harmful={fmt(ag['kappa_harmful'])}")
    print(f"Gold set: {len(gold)} items {result['gold_distribution']}; "
          f"{len(disagreements)} disagreements still to adjudicate in {adj_path.relative_to(ROOT)}")
    print(f"\n{'instrument':<16} {'n':>4}   any-MH P / R / F1        harmful P / R / F1")
    for ins in result["instruments"]:
        m, h = ins["any_mh"], ins["harmful"]
        print(f"{ins['instrument']:<16} {ins['n']:>4}   {fmt(m['precision'])} / {fmt(m['recall'])} / {fmt(m['f1'])}"
              f"       {fmt(h['precision'])} / {fmt(h['recall'])} / {fmt(h['f1'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args())
