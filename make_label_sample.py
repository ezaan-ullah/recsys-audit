"""Draw the human-validation sample and write the offline labeling page.

    python make_label_sample.py                # ~150 items from data/raw
    python make_label_sample.py --dry-run      # from pilot data
    python make_label_sample.py --n 200

Stratified so rare classes are present: up to half the sample comes from items that
Gemini's outcome labels or the live trigger marked as positive; the rest is random. The
order is shuffled and no machine label is shown, so labelers are blind to strata.

Each labeler opens data/labels/label.html in a browser (no server needed), labels, and
downloads labels_<name>.json into data/labels/human/. Then run validate_labels.py.
"""
import argparse
import hashlib
import json
import os
import random
from pathlib import Path

from audit.config import ROOT, load_config
from audit.data import data_root, item_meta, latest_labels, load_meta_cache, load_rows
from audit.htmlpage import BASE_CSS, esc, md_to_html
from audit.media import MediaStore
from label_items import labels_path

POSITIVE = {"adjacent", "mh_distress", "harmful", "blocked"}


def draw_sample(vids: list[str], positives: set[str], n: int, seed: str) -> list[str]:
    rng = random.Random(seed)
    pos = sorted(v for v in vids if v in positives)
    neg = sorted(v for v in vids if v not in positives)
    rng.shuffle(pos)
    rng.shuffle(neg)
    take_pos = pos[: min(len(pos), n // 2)]
    rest = (pos[len(take_pos):] + neg)
    rng.shuffle(rest)
    sample = take_pos + rest[: n - len(take_pos)]
    rng.shuffle(sample)
    return sample


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Short Labeling</title>
<style>__CSS__
.item { display: grid; grid-template-columns: minmax(0, 260px) minmax(0, 1fr); gap: 16px; }
@media (max-width: 640px) { .item { grid-template-columns: minmax(0, 1fr); } }
.imgs { display: flex; gap: 6px; flex-wrap: wrap; } .imgs img { width: 120px; border-radius: 6px; }
.choices label { display: inline-block; margin: 4px 12px 4px 0; } .flags label { margin-right: 12px; color: var(--ink2); }
.desc { white-space: pre-wrap; max-height: 7em; overflow: auto; font-size: 13px; color: var(--ink2); }
.bar { position: sticky; top: 0; z-index: 2; background: var(--bg); padding: 10px 0; border-bottom: 1px solid var(--line);
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.done { border-color: var(--accent); }
</style></head><body><main>
<h1>Short labeling</h1>
<p>Label each Short with the codebook below. Images are blurred: click one to reveal it, click again to hide it.
Take breaks; stop if the content affects you. If you need the video itself, use <em>Open</em> in a private window
signed out of any account, so your own recommendations are not affected.</p>
<details class="card" open><summary><strong>Codebook</strong></summary>__CODEBOOK__</details>
<div class="bar"><label>Your name <input id="name" placeholder="e.g. ayesha" required></label>
<span id="progress" class="muted"></span><button class="primary" id="dl">Download labels</button></div>
<div id="items"></div>
</main><script>
const ITEMS = __ITEMS__, SAMPLE_ID = "__SAMPLE__", CODEBOOK_SHA = "__CBSHA__";
const LABELS = ["none", "adjacent", "mh_distress", "harmful"], FLAGS = ["is_ad", "supportive", "unclear"];
let labels = {};
const nameEl = document.getElementById("name");
const storeKey = () => `labels:${SAMPLE_ID}:${nameEl.value.trim()}`;
function save() { try { localStorage.setItem(storeKey(), JSON.stringify(labels)); localStorage.setItem(`labeler:${SAMPLE_ID}`, nameEl.value.trim()); } catch (e) {} }
function load() { try { labels = JSON.parse(localStorage.getItem(storeKey()) || "{}"); } catch (e) { labels = {}; } render(); }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function progress() {
  const n = ITEMS.filter(it => labels[it.video_id] && labels[it.video_id].label).length;
  document.getElementById("progress").textContent = `${n} / ${ITEMS.length} labeled`;
}
function render() {
  const root = document.getElementById("items");
  root.innerHTML = ITEMS.map((it, i) => {
    const l = labels[it.video_id] || {};
    return `<div class="card item ${l.label ? "done" : ""}" id="c${i}">
      <div><div class="muted">#${i + 1}</div><div class="imgs">${it.images.map(src =>
        `<img class="blur" src="${esc(src)}" alt="Short image (click to reveal)">`).join("") || '<span class="muted">no image captured</span>'}</div></div>
      <div><strong>${esc(it.title)}</strong><div class="muted">${esc(it.channel)} · ${esc(it.tags)}
        · <a href="https://www.youtube.com/shorts/${esc(it.video_id)}" target="_blank" rel="noopener">Open</a></div>
        <div class="desc">${esc(it.description)}</div>
        <div class="choices">${LABELS.map(v => `<label><input type="radio" name="l${i}" value="${v}" ${l.label === v ? "checked" : ""}> ${v}</label>`).join("")}</div>
        <div class="flags">${FLAGS.map(f => `<label><input type="checkbox" data-f="${f}" ${l[f] ? "checked" : ""}> ${f}</label>`).join("")}
          <input data-f="note" placeholder="note (optional)" value="${esc(l.note || "")}"></div></div></div>`;
  }).join("");
  root.querySelectorAll("img.blur").forEach(img => img.addEventListener("click", () => img.classList.toggle("shown")));
  ITEMS.forEach((it, i) => {
    const card = document.getElementById(`c${i}`);
    card.addEventListener("change", e => {
      const l = labels[it.video_id] = labels[it.video_id] || {};
      if (e.target.type === "radio") { l.label = e.target.value; card.classList.add("done"); }
      else if (e.target.dataset.f === "note") l.note = e.target.value;
      else l[e.target.dataset.f] = e.target.checked;
      save(); progress();
    });
  });
  progress();
}
document.getElementById("dl").addEventListener("click", () => {
  const who = nameEl.value.trim();
  if (!who) { nameEl.focus(); alert("Enter your name first."); return; }
  const blob = new Blob([JSON.stringify({labeler: who, sample_id: SAMPLE_ID, codebook_sha: CODEBOOK_SHA,
    saved_utc: new Date().toISOString(), labels}, null, 1)], {type: "application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `labels_${who.replace(/[^a-z0-9_-]+/gi, "_")}.json`; a.click();
});
nameEl.addEventListener("change", load);
try { nameEl.value = localStorage.getItem(`labeler:${SAMPLE_ID}`) || ""; } catch (e) {}
load();
</script></body></html>"""


def main(args) -> None:
    cfg = load_config()
    rows = load_rows(data_root(cfg, args.dry_run))
    rows_by_vid = {}
    for r in rows:
        if not r.get("is_ad"):
            rows_by_vid.setdefault(r["video_id"], r)
    if not rows_by_vid:
        raise SystemExit("No collected items found.")
    gem = latest_labels(labels_path(cfg, args.dry_run))
    trig_pos = {r["video_id"] for r in rows if r.get("trigger_label") in ("adjacent", "harmful", "blocked")}
    positives = {v for v, r in gem.items() if r.get("label") in POSITIVE} | trig_pos
    sample = draw_sample(sorted(rows_by_vid), positives, args.n, f"{cfg['study']['seed']}|sample|{args.n}")

    media = MediaStore(ROOT / cfg["paths"]["media_dir"])
    meta_cache = load_meta_cache(cfg)
    labels_dir = ROOT / cfg["paths"]["labels_dir"]
    (labels_dir / "human").mkdir(parents=True, exist_ok=True)
    items = []
    for v in sample:
        m = item_meta(v, rows_by_vid, meta_cache)
        imgs = [media.thumb_path(v)] + media.frame_paths(v)[:2]
        items.append({"video_id": v, "title": m.get("title") or "", "channel": m.get("channel_title") or "",
                      "tags": ", ".join((m.get("tags") or [])[:12]), "description": (m.get("description") or "")[:600],
                      "images": [Path(os.path.relpath(p, labels_dir)).as_posix() for p in imgs if p.exists()]})
    codebook = (ROOT / cfg["labeler"]["codebook"]).read_text(encoding="utf-8")
    cb_sha = hashlib.sha256(codebook.encode()).hexdigest()[:12]
    sample_id = hashlib.sha256(",".join(sample).encode()).hexdigest()[:10]
    (labels_dir / "sample.json").write_text(json.dumps(
        {"sample_id": sample_id, "codebook_sha": cb_sha, "dry_run": args.dry_run, "video_ids": sample,
         "n_positive_stratum": len([v for v in sample if v in positives])}, indent=1), encoding="utf-8")
    page = (PAGE.replace("__CSS__", BASE_CSS).replace("__CODEBOOK__", md_to_html(codebook))
                .replace("__ITEMS__", json.dumps(items, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__SAMPLE__", sample_id).replace("__CBSHA__", cb_sha))
    (labels_dir / "label.html").write_text(page, encoding="utf-8")
    missing = sum(not it["images"] for it in items)
    print(f"Sample {sample_id}: {len(sample)} items ({len([v for v in sample if v in positives])} from the positive stratum, "
          f"{missing} without any image).\nOpen {(labels_dir / 'label.html').relative_to(ROOT)} in a browser; "
          f"save each labeler's download into {(labels_dir / 'human').relative_to(ROOT)}/.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--n", type=int, default=150)
    main(ap.parse_args())
