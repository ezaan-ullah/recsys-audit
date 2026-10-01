"""Find candidate seed Shorts and write a review page for choosing the two seed lists.

    python build_seeds.py

Uses the researcher's own YouTube Data API key (YT_API_KEY), never a study account, so
no sock puppet ever searches. Each search costs 100 of the 10,000 daily quota units.
Candidates are pre-labeled with the live trigger classifier (Gemini if configured, else
keywords); harmful or refused items are dropped. Review data/seeds/review.html (images
blurred until clicked), tick ~10 per arm, press Export, and paste the two lists into
seeds/treatment.txt and seeds/control.txt. Export pairs each treatment seed with the
unused control seed closest in duration.
"""
import asyncio
import json
import os
import sys

import httpx

from audit.classifier import TriggerClassifier
from audit.config import ROOT, load_config
from audit.gemini import GeminiClient, RateLimiter
from audit.htmlpage import BASE_CSS
from audit.media import MediaStore
from audit.metadata import parse_iso_duration
from audit.policy import Keywords

API = "https://www.googleapis.com/youtube/v3"
ADJACENT_QUERIES = ["sad shayari", "sad status urdu", "udaas status", "dard bhari shayari", "tanhai shayari",
                    "broken heart status", "sad poetry urdu", "اداس شاعری", "sad edit", "alone sad status"]
NEUTRAL_QUERIES = ["recipe shorts", "cricket shorts", "street food pakistan", "funny animals", "car shorts",
                   "football skills", "science experiment", "travel pakistan", "nature shorts", "cooking hacks"]
PER_QUERY = 10
DURATION_RANGE = (8, 60)


async def search(client, key, q):
    r = await client.get(f"{API}/search", params={
        "part": "snippet", "q": q, "type": "video", "videoDuration": "short", "regionCode": "PK",
        "maxResults": PER_QUERY, "safeSearch": "moderate", "key": key})
    r.raise_for_status()
    return [it["id"]["videoId"] for it in r.json().get("items", [])]


async def details(client, key, ids):
    out = []
    for i in range(0, len(ids), 50):
        r = await client.get(f"{API}/videos", params={
            "part": "snippet,contentDetails,statistics", "id": ",".join(ids[i:i + 50]), "key": key})
        r.raise_for_status()
        for it in r.json().get("items", []):
            sn, cd = it["snippet"], it["contentDetails"]
            out.append({"video_id": it["id"], "title": sn.get("title"), "description": sn.get("description"),
                        "tags": sn.get("tags", []), "channel_title": sn.get("channelTitle"),
                        "duration_s": parse_iso_duration(cd.get("duration")),
                        "views": int(it.get("statistics", {}).get("viewCount", 0))})
    return out


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Seed Review</title>
<style>__CSS__
.cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
@media (max-width: 760px) { .cols { grid-template-columns: minmax(0, 1fr); } }
.cand { display: flex; gap: 10px; align-items: flex-start; padding: 8px 0; border-bottom: 1px solid var(--line); }
.cand img { width: 96px; border-radius: 6px; flex: none; } textarea { width: 100%; height: 220px; font: 12px monospace; }
</style></head><body><main>
<h1>Seed review</h1>
<p>Tick about 10 per arm. Treatment seeds must be sad / lonely / heartbreak themed and <strong>never</strong> about
suicide or self-harm. Control seeds must have no sadness or mental-health theme. Images are blurred; click to reveal.</p>
<div class="cols"><section><h2>Treatment candidates</h2><div id="t"></div></section>
<section><h2>Control candidates</h2><div id="c"></div></section></div>
<p><button class="primary" id="ex">Export duration-matched lists</button> <span id="msg" class="muted"></span></p>
<div class="cols"><div><h3>seeds/treatment.txt</h3><textarea id="ot" readonly></textarea></div>
<div><h3>seeds/control.txt</h3><textarea id="oc" readonly></textarea></div></div>
</main><script>
const C = __DATA__;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
for (const arm of ["t", "c"]) document.getElementById(arm).innerHTML = C[arm].map((x, i) => `<label class="cand">
  <input type="checkbox" data-arm="${arm}" data-i="${i}"><img class="blur" src="${esc(x.thumb)}" alt="thumbnail">
  <span><strong>${esc(x.title)}</strong><br><span class="muted">${esc(x.channel_title)} · ${x.duration_s}s ·
  label: ${esc(x.label)} (${esc(x.source)}) · ${x.views.toLocaleString()} views</span></span></label>`).join("");
document.querySelectorAll("img.blur").forEach(im => im.addEventListener("click", e => { e.preventDefault(); im.classList.toggle("shown"); }));
document.getElementById("ex").addEventListener("click", () => {
  const pick = arm => [...document.querySelectorAll(`input[data-arm=${arm}]:checked`)].map(b => C[arm][+b.dataset.i]);
  const t = pick("t").sort((a, b) => a.duration_s - b.duration_s), pool = pick("c");
  if (t.length > pool.length) { document.getElementById("msg").textContent = "Tick at least as many control as treatment seeds."; return; }
  const lt = [], lc = [], warn = [];
  for (const x of t) {
    let bi = 0; pool.forEach((y, i) => { if (Math.abs(y.duration_s - x.duration_s) < Math.abs(pool[bi].duration_s - x.duration_s)) bi = i; });
    const y = pool.splice(bi, 1)[0];
    if (Math.abs(y.duration_s - x.duration_s) > 0.2 * x.duration_s) warn.push(x.video_id);
    lt.push(`${x.video_id}  # ${x.duration_s}s ${x.title.slice(0, 60)}`); lc.push(`${y.video_id}  # ${y.duration_s}s ${y.title.slice(0, 60)}`);
  }
  document.getElementById("ot").value = lt.join("\\n"); document.getElementById("oc").value = lc.join("\\n");
  document.getElementById("msg").textContent = warn.length ? `${warn.length} pair(s) differ by more than 20% in duration.` : "All pairs within 20% duration.";
});
</script></body></html>"""


async def main() -> None:
    cfg = load_config()
    yt_key = os.environ.get(cfg["youtube"]["api_key_env"])
    if not yt_key:
        sys.exit(f"Set {cfg['youtube']['api_key_env']} (the researcher's YouTube Data API key).")
    out_dir = ROOT / cfg["paths"]["data_dir"] / "seeds"
    out_dir.mkdir(parents=True, exist_ok=True)
    media = MediaStore(ROOT / cfg["paths"]["media_dir"])
    ccfg = cfg["classifier"]
    gkey = os.environ.get(ccfg["api_key_env"])
    gemini = None
    if ccfg.get("model") and gkey:
        gemini = GeminiClient(gkey, ccfg["model"], ccfg.get("thinking_budget"),
                              RateLimiter(ccfg["rpm"], ccfg["rpd"], ROOT / cfg["paths"]["gemini_usage"]))
    else:
        print("WARNING: no Gemini model/key; candidates are pre-labeled by keywords only.")
    clf = TriggerClassifier(Keywords.from_config(cfg), gemini, (ROOT / ccfg["prompt"]).read_text(encoding="utf-8"),
                            ROOT / cfg["paths"]["trigger_cache"], media)

    found = {"t": [], "c": []}
    async with httpx.AsyncClient(timeout=15) as client:
        for arm, queries in (("t", ADJACENT_QUERIES), ("c", NEUTRAL_QUERIES)):
            ids = []
            for q in queries:
                ids += [i for i in await search(client, yt_key, q) if i not in ids]
            for m in await details(client, yt_key, ids):
                if not m["duration_s"] or not DURATION_RANGE[0] <= m["duration_s"] <= DURATION_RANGE[1]:
                    continue
                v = await clf.classify(m, 60)
                if v.excluded:
                    continue
                want = v.adjacent if arm == "t" else not v.adjacent
                if not want:
                    continue
                if await media.thumbnail(m["video_id"]) is None:
                    continue
                found[arm].append({**m, "label": v.label, "source": v.source, "description": None,
                                   "thumb": os.path.relpath(media.thumb_path(m["video_id"]), out_dir)})
    await media.close()
    (out_dir / "candidates.json").write_text(json.dumps(found, indent=1, ensure_ascii=False), encoding="utf-8")
    page = PAGE.replace("__CSS__", BASE_CSS).replace("__DATA__", json.dumps(found, ensure_ascii=False).replace("</", "<\\/"))
    (out_dir / "review.html").write_text(page, encoding="utf-8")
    print(f"{len(found['t'])} treatment and {len(found['c'])} control candidates -> "
          f"{(out_dir / 'review.html').relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
