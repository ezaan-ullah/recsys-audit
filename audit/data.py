"""Loading collected rows, session logs, metadata, and labels for offline analysis."""
import json
from pathlib import Path

from .config import ROOT


def read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def data_root(cfg: dict, dry_run: bool) -> Path:
    return ROOT / cfg["paths"]["data_dir"] / ("dryrun" if dry_run else "raw")


def slot_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*_s[0-9][0-9]") if p.is_dir()) if root.exists() else []


def load_rows(root: Path) -> list[dict]:
    """Every item row of the current attempt in every slot (superseded attempts are excluded)."""
    rows = []
    for d in slot_dirs(root):
        for f in sorted(d.glob("*.jsonl")):
            if not f.name.startswith("_"):
                rows.extend(read_jsonl(f))
    return rows


def load_session_logs(root: Path) -> list[dict]:
    """The latest log row per account per slot."""
    out = []
    for d in slot_dirs(root):
        latest = {}
        for r in read_jsonl(d / "_session_log.jsonl"):
            latest[r["account_id"]] = r
        out.extend(latest.values())
    return out


def load_meta_cache(cfg: dict) -> dict[str, dict]:
    return {r["video_id"]: r for r in read_jsonl(ROOT / cfg["paths"]["meta_cache"]) if "video_id" in r}


def item_meta(video_id: str, rows_by_vid: dict[str, dict], meta_cache: dict[str, dict]) -> dict:
    """Full Data API metadata when cached, else what the collection row recorded."""
    if video_id in meta_cache:
        return meta_cache[video_id]
    r = rows_by_vid.get(video_id, {})
    return {"video_id": video_id, "title": r.get("title"), "channel_title": r.get("channel_title"),
            "tags": r.get("tags") or [], "description": None, "duration_s": r.get("duration_s")}


def latest_labels(path: Path, key: str | None = None) -> dict[str, dict]:
    """video_id -> latest label record (optionally only records made with `key`)."""
    out = {}
    for r in read_jsonl(path):
        if key is None or r.get("key") == key:
            out[r["video_id"]] = r
    return out
