"""Offline outcome labeling with Gemini, using the codebook (docs/codebook.md).

    python label_items.py               # label every collected organic item not yet labeled
    python label_items.py --dry-run     # pilot data instead
    python label_items.py --max-requests 200

This is the measurement instrument, separate from the live trigger: different prompt,
richer inputs (thumbnail + captured frames + full metadata), four classes. Its accuracy
is measured against the human gold set by validate_labels.py. Resumable: items already
labeled with the same model and prompt are skipped. Stops cleanly at the daily quota.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys

from audit.classifier import item_prompt
from audit.config import ROOT, load_config
from audit.data import data_root, latest_labels, load_meta_cache, load_rows, item_meta
from audit.gemini import GeminiClient, RateLimiter
from audit.media import MediaStore

LABELS = ("none", "adjacent", "mh_distress", "harmful")
SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "is_ad": {"type": "boolean"},
        "supportive": {"type": "boolean"},
        "unclear": {"type": "boolean"},
        "language": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "rationale": {"type": "string"},
    },
    "required": ["label", "is_ad", "supportive", "unclear", "language", "confidence", "rationale"],
}


def outcome_prompt(cfg: dict) -> str:
    lcfg = cfg["labeler"]
    codebook = (ROOT / lcfg["codebook"]).read_text(encoding="utf-8")
    return (ROOT / lcfg["prompt"]).read_text(encoding="utf-8").replace("{{CODEBOOK}}", codebook)


def labeler_key(cfg: dict) -> str:
    return f"{cfg['labeler']['model']}|{hashlib.sha256(outcome_prompt(cfg).encode()).hexdigest()[:12]}"


def labels_path(cfg: dict, dry_run: bool):
    return ROOT / cfg["paths"]["labels_dir"] / ("gemini_outcome_dryrun.jsonl" if dry_run else "gemini_outcome.jsonl")


async def main(args) -> None:
    cfg = load_config()
    lcfg = cfg["labeler"]
    key = os.environ.get(lcfg["api_key_env"])
    if not lcfg.get("model") or not key:
        sys.exit(f"Set labeler.model in config.yaml and {lcfg['api_key_env']} in the environment.")
    out_path = labels_path(cfg, args.dry_run)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lkey = labeler_key(cfg)
    done = latest_labels(out_path, lkey)

    rows = load_rows(data_root(cfg, args.dry_run))
    rows_by_vid = {}
    for r in rows:
        if not r.get("is_ad"):
            rows_by_vid.setdefault(r["video_id"], r)
    todo = [v for v in rows_by_vid if v not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(rows_by_vid)} unique organic items, {len(done)} already labeled, {len(todo)} to label "
          f"(model={lcfg['model']})")
    if not todo:
        return

    limiter = RateLimiter(lcfg["rpm"], lcfg["rpd"], ROOT / cfg["paths"]["gemini_usage"])
    gemini = GeminiClient(key, lcfg["model"], lcfg.get("thinking_budget"), limiter, timeout_s=60)
    media = MediaStore(ROOT / cfg["paths"]["media_dir"])
    meta_cache = load_meta_cache(cfg)
    system = outcome_prompt(cfg)
    counts, sent = {}, 0
    try:
        for i, vid in enumerate(todo, 1):
            if args.max_requests and sent >= args.max_requests:
                print(f"Stopped at --max-requests {args.max_requests}.")
                break
            if limiter.used_today(lcfg["model"]) >= limiter.rpd:
                print("Daily request quota reached; re-run tomorrow to continue.")
                break
            parts = []
            thumb = await media.thumbnail(vid)
            if thumb:
                parts.append(thumb)
            frames = media.frame_paths(vid)[:2]
            parts.extend(p.read_bytes() for p in frames)
            parts.append(item_prompt(item_meta(vid, rows_by_vid, meta_cache), max_desc=3000))
            for _ in range(3):          # a 429 from the server: back off and retry the same item
                res = await gemini.generate_json(system=system, parts=parts, schema=SCHEMA)
                sent += 1
                if res.status != "ratelimited" or limiter.used_today(lcfg["model"]) >= limiter.rpd:
                    break
                await asyncio.sleep(30)
            if res.status not in ("ok", "blocked"):
                print(f"  {vid}: {res.status} {res.detail}")
                continue
            rec = {"video_id": vid, "key": lkey, "status": res.status,
                   **(res.data if res.status == "ok" else {"label": "blocked"}),
                   "n_images": len(parts) - 1, "had_thumbnail": thumb is not None, "n_frames": len(frames),
                   "detail": res.detail}
            if rec["label"] not in LABELS + ("blocked",):
                print(f"  {vid}: invalid label {rec['label']!r}")
                continue
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            counts[rec["label"]] = counts.get(rec["label"], 0) + 1
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}  {counts}")
    finally:
        await media.close()
    print(f"Labeled this run: {counts}  ->  {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="label pilot data (data/dryrun)")
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--limit", type=int, help="label at most this many items")
    asyncio.run(main(ap.parse_args()))
