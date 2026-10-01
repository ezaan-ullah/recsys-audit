"""Drift analysis and the MVP report.

    python analyze.py              # data/raw  -> reports/mvp_report.html
    python analyze.py --dry-run    # pilot data -> reports/mvp_report_dryrun.html

Outcome per feed item (ads and seed views excluded): Gemini's outcome label
(label_items.py), validated against the human gold set (validate_labels.py). The keyword
measure is reported alongside as a sensitivity check.

Estimands, per yoked pair (treatment minus control, so time, IP, and dose are matched):
  diff(s)   = share of MH-themed items for treatment - control, in session s
  DiD       = mean diff over treatment sessions - mean diff over baseline sessions
  persist   = mean diff over washout sessions - mean diff over baseline sessions
  drift at  = first treatment session whose diff exceeds the largest baseline diff + margin
With 2-3 pairs these are descriptive; the sign-flip p-value is shown only for completeness.
"""
import argparse
import base64
import io
import json
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from audit.config import ROOT, load_config, load_protocol  # noqa: E402
from audit.data import data_root, item_meta, latest_labels, load_meta_cache, load_rows, load_session_logs  # noqa: E402
from audit.htmlpage import BASE_CSS, esc  # noqa: E402
from audit.metrics import MH, sign_flip_p  # noqa: E402
from audit.policy import Keywords  # noqa: E402
from label_items import labeler_key, labels_path  # noqa: E402

ARM = {"treatment": "#2a78d6", "control": "#eb6834"}     # categorical slots 1-2 (validated palette)
INK, INK2, MUTED, GRID, AXIS, SURFACE, BAND = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb", "#f0efec"
PHASE_ORDER = ("baseline", "seed", "treatment", "washout")


def item_frame(cfg: dict, dry_run: bool) -> tuple[pd.DataFrame, str]:
    rows = load_rows(data_root(cfg, dry_run))
    if not rows:
        raise SystemExit("No collected rows.")
    rows_by_vid = {}
    for r in rows:
        rows_by_vid.setdefault(r["video_id"], r)
    key = labeler_key(cfg) if cfg["labeler"].get("model") else None
    gem = latest_labels(labels_path(cfg, dry_run), key)
    kw, meta_cache = Keywords.from_config(cfg), load_meta_cache(cfg)
    kw_cache = {}
    recs = []
    for r in rows:
        v = r["video_id"]
        if v not in kw_cache:
            adj, exc = kw.match(item_meta(v, rows_by_vid, meta_cache))
            kw_cache[v] = (adj is not None or exc is not None, exc is not None)
        g = gem.get(v, {})
        label = g.get("label")
        recs.append({
            "account_id": r["account_id"], "pair": r.get("pair"), "group": r.get("group"), "phase": r["phase"],
            "session": r["session"], "organic_index": r.get("organic_index"), "video_id": v,
            "is_ad": bool(r.get("is_ad")), "long": bool(r.get("long")), "reason": r.get("reason"),
            "dwell_s": r.get("dwell_s") or 0.0, "exposure_s": r.get("exposure_s"),
            "label": label,
            "mh": None if label is None else (label in MH or label == "blocked"),
            "harmful": None if label is None else (label in ("harmful", "blocked")),
            "kw_mh": kw_cache[v][0], "kw_harmful": kw_cache[v][1],
            "trigger_source": r.get("trigger_source"), "interstitial": bool(r.get("interstitial")),
        })
    df = pd.DataFrame(recs)
    measure = "gemini" if df["label"].notna().any() else "keywords"
    return df, measure


def account_sessions(feed: pd.DataFrame, measure: str) -> pd.DataFrame:
    mh, harm = ("mh", "harmful") if measure == "gemini" else ("kw_mh", "kw_harmful")
    g = feed.groupby(["pair", "group", "account_id", "phase", "session"], dropna=False)
    out = g.agg(items=("video_id", "size"),
                labeled=("label", lambda s: s.notna().sum()),
                share_mh=(mh, lambda s: s.dropna().astype(float).mean()),
                share_harmful=(harm, lambda s: s.dropna().astype(float).mean()),
                share_kw=("kw_mh", "mean"),
                interstitials=("interstitial", "sum")).reset_index()
    return out.sort_values(["session", "pair", "group"])


def pair_diffs(acct: pd.DataFrame) -> pd.DataFrame:
    wide = acct.pivot_table(index=["pair", "phase", "session"], columns="group",
                            values=["share_mh", "share_harmful", "share_kw"]).reset_index()
    out = pd.DataFrame({"pair": wide["pair"], "phase": wide["phase"], "session": wide["session"]})
    for m in ("share_mh", "share_harmful", "share_kw"):
        if (m, "treatment") in wide and (m, "control") in wide:
            out[f"diff_{m[6:]}"] = wide[(m, "treatment")] - wide[(m, "control")]
    return out.sort_values(["pair", "session"])


def pair_summary(diffs: pd.DataFrame, margin: float) -> pd.DataFrame:
    rows = []
    for pid, d in diffs.groupby("pair"):
        base = d[d.phase == "baseline"]["diff_mh"].dropna()
        treat = d[d.phase == "treatment"].dropna(subset=["diff_mh"])
        wash = d[d.phase == "washout"]["diff_mh"].dropna()
        b = base.mean() if len(base) else None
        drift_at = None
        if len(base):
            over = treat[treat["diff_mh"] > base.max() + margin]
            drift_at = int(over["session"].iloc[0]) if len(over) else None
        rows.append({"pair": pid, "baseline_diff": b,
                     "treatment_diff": treat["diff_mh"].mean() if len(treat) else None,
                     "washout_diff": wash.mean() if len(wash) else None,
                     "DiD": (treat["diff_mh"].mean() - b) if (b is not None and len(treat)) else None,
                     "persistence": (wash.mean() - b) if (b is not None and len(wash)) else None,
                     "drift_session": drift_at,
                     "harmful_DiD": (d[d.phase == "treatment"]["diff_harmful"].mean()
                                     - d[d.phase == "baseline"]["diff_harmful"].mean())
                     if "diff_harmful" in d else None})
    return pd.DataFrame(rows)


def dose_table(df: pd.DataFrame) -> pd.DataFrame:
    org = df[~df.is_ad]
    t = org.groupby(["pair", "group", "account_id", "phase"]).agg(
        items=("video_id", "size"), long_dwells=("long", "sum"), excluded=("reason", lambda s: (s == "excluded").sum()),
        watch_min=("dwell_s", lambda s: round(s.sum() / 60, 1))).reset_index()
    t["phase"] = pd.Categorical(t["phase"], PHASE_ORDER, ordered=True)
    return t.sort_values(["pair", "phase", "group"])


# ---- figures -------------------------------------------------------------

def _style(ax, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_ylabel(ylabel, color=INK2, fontsize=10)


def _phase_bands(ax, proto: dict, sessions: list[int]) -> None:
    """Label every phase in the plotted range; shade the non-treatment ones."""
    lo, hi = min(sessions), max(sessions)
    by_phase = {}
    for s in proto["sessions"]:
        if lo <= s["n"] <= hi:
            by_phase.setdefault(s["phase"], []).append(s["n"])
    for phase, ns in by_phase.items():
        if phase != "treatment":
            ax.axvspan(min(ns) - 0.5, max(ns) + 0.5, color=BAND, zorder=0, linewidth=0)
        ax.text((min(ns) + max(ns)) / 2, 1.02, phase, transform=ax.get_xaxis_transform(),
                ha="center", va="bottom", fontsize=9, color=MUTED)


def _end_labels(ax, ends: list[tuple[str, float, float]]) -> None:
    """Direct labels at line ends, nudged apart when they would collide."""
    lo, hi = ax.get_ylim()
    ends = sorted(ends, key=lambda e: e[2])
    for i, (text, x, y) in enumerate(ends):
        dy = 0
        if len(ends) == 2 and abs(ends[1][2] - ends[0][2]) < 0.06 * (hi - lo):
            dy = -7 if i == 0 else 7
        ax.annotate(text, (x, y), xytext=(6, dy), textcoords="offset points", color=INK2, fontsize=9, va="center")


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=144, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def fig_share(acct: pd.DataFrame, proto: dict, col: str, ylabel: str) -> str:
    fig, ax = plt.subplots(figsize=(8, 3.6))
    _style(ax, ylabel)
    sessions = sorted(acct["session"].unique())
    _phase_bands(ax, proto, sessions)
    ends = []
    for grp, color in ARM.items():
        a = acct[acct.group == grp]
        for _, s in a.groupby("account_id"):
            ax.plot(s["session"], s[col], color=color, alpha=0.3, linewidth=1)
        m = a.groupby("session")[col].mean()
        if len(m):
            ax.plot(m.index, m.values, color=color, linewidth=2, marker="o", markersize=6,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, label=f"{grp} (mean)")
            ends.append((grp, m.index[-1], m.values[-1]))
    ax.set_xticks(sessions)
    ax.set_xlabel("session", color=INK2, fontsize=10)
    ax.set_ylim(bottom=0)
    _end_labels(ax, ends)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK2, loc="upper left")
    return _png(fig)


def fig_diff(diffs: pd.DataFrame, proto: dict) -> str:
    fig, ax = plt.subplots(figsize=(8, 3.4))
    _style(ax, "treatment − control share")
    sessions = sorted(diffs["session"].unique())
    _phase_bands(ax, proto, sessions)
    ax.axhline(0, color=AXIS, linewidth=1)
    for pid, d in diffs.groupby("pair"):
        ax.plot(d["session"], d["diff_mh"], color=MUTED, linewidth=1, alpha=0.8)
        ax.annotate(pid, (d["session"].iloc[-1], d["diff_mh"].iloc[-1]), xytext=(5, 0),
                    textcoords="offset points", fontsize=8, color=MUTED, va="center")
    m = diffs.groupby("session")["diff_mh"].mean()
    ax.plot(m.index, m.values, color=ARM["treatment"], linewidth=2, marker="o", markersize=6,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label="mean over pairs")
    ax.set_xticks(sessions)
    ax.set_xlabel("session", color=INK2, fontsize=10)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK2, loc="upper left")
    return _png(fig)


def fig_position(feed: pd.DataFrame, col: str, bin_size: int) -> str | None:
    t = feed[(feed.phase == "treatment") & feed[col].notna()].copy()
    if t.empty:
        return None
    t["bin"] = (t["organic_index"] // bin_size) * bin_size
    fig, ax = plt.subplots(figsize=(8, 3.2))
    _style(ax, "share MH-themed")
    for grp, color in ARM.items():
        m = t[t.group == grp].groupby("bin")[col].apply(lambda s: s.astype(float).mean())
        if len(m):
            ax.plot(m.index, m.values, color=color, linewidth=2, marker="o", markersize=6,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, label=grp)
    ax.set_xlabel(f"position in session (bins of {bin_size} items), treatment-phase sessions pooled",
                  color=INK2, fontsize=10)
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK2, loc="upper left")
    return _png(fig)


# ---- report --------------------------------------------------------------

def _table(df: pd.DataFrame, digits: int = 3) -> str:
    if df is None or df.empty:
        return '<p class="muted">No data.</p>'
    return '<div class="scroll">' + df.to_html(index=False, na_rep="–", float_format=lambda x: f"{x:.{digits}f}",
                                                border=0, escape=True) + "</div>"


def _fig(b64: str | None, alt: str, empty: str = "Not enough data for this figure yet.") -> str:
    return f'<img src="data:image/png;base64,{b64}" alt="{esc(alt)}" style="width:100%;height:auto">' if b64 else \
        f'<p class="muted">{esc(empty)}</p>'


def _fmt(x, pct=False):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "–"
    return f"{x * 100:+.1f} pp" if pct else f"{x:.3f}"


def validation_html(cfg: dict) -> str:
    path = ROOT / cfg["paths"]["labels_dir"] / "validation.json"
    if not path.exists():
        return ('<p class="muted">No human validation yet: run make_label_sample.py, have two people label, then '
                'validate_labels.py. Until then, the outcome measure is unvalidated.</p>')
    v = json.loads(path.read_text(encoding="utf-8"))
    parts = []
    if "agreement" in v:
        a = v["agreement"]
        parts.append(f"<p>Two labelers ({esc(', '.join(v['labelers']))}), {a['n_both']} items: "
                     f"Cohen's κ = {_fmt(a['kappa_4class'])} (4-class), {_fmt(a['kappa_any_mh'])} (any MH theme), "
                     f"{_fmt(a['kappa_harmful'])} (harmful). Gold set: {v['gold_n']} items; "
                     f"{v['unadjudicated']} disagreements not yet adjudicated.</p>")
    rows = []
    for ins in v["instruments"]:
        m, h = ins["any_mh"], ins["harmful"]
        rows.append({"instrument": ins["instrument"], "n": ins["n"],
                     "any-MH precision": m["precision"], "any-MH recall": m["recall"], "any-MH F1": m["f1"],
                     "harmful precision": h["precision"], "harmful recall": h["recall"], "harmful positives": h["positives"]})
    parts.append(_table(pd.DataFrame(rows), 2))
    return "\n".join(parts)


def quality_table(logs: list[dict]) -> pd.DataFrame:
    if not logs:
        return pd.DataFrame()
    keep = ["session", "phase", "pair", "group", "account_id", "status", "items", "ads", "long_rate", "gemini_rate",
            "data_api_rate", "active_video_rate", "playing_rate", "frames_rate", "interstitials", "flags"]
    t = pd.DataFrame(logs)
    t = t[[k for k in keep if k in t.columns]].copy()
    if "flags" in t:
        t["flags"] = t["flags"].apply(lambda f: ", ".join(f) if isinstance(f, list) else "")
    return t.sort_values(["session", "pair", "group"])


def main(args) -> None:
    cfg = load_config()
    proto = load_protocol(cfg)
    df, measure = item_frame(cfg, args.dry_run)
    feed = df[(~df.is_ad) & (df.phase != "seed")]
    acct = account_sessions(feed, measure)
    diffs = pair_diffs(acct)
    margin = float(cfg["analysis"]["drift_margin"])
    summ = pair_summary(diffs, margin) if "diff_mh" in diffs else pd.DataFrame()
    dids = [x for x in summ.get("DiD", []) if x is not None and not pd.isna(x)]
    p = sign_flip_p(dids)
    mean_did = sum(dids) / len(dids) if dids else None
    logs = load_session_logs(data_root(cfg, args.dry_run))
    col = "mh" if measure == "gemini" else "kw_mh"
    labeled_share = df.loc[~df.is_ad, "label"].notna().mean() if len(df) else 0

    figs = {
        "share": fig_share(acct, proto, "share_mh", "share MH-themed") if len(acct) else None,
        "harm": fig_share(acct, proto, "share_harmful", "share harmful")
        if len(acct) and acct["share_harmful"].fillna(0).max() > 0 else None,
        "diff": fig_diff(diffs, proto) if "diff_mh" in diffs and diffs["diff_mh"].notna().any() else None,
        "pos": fig_position(feed, col, int(cfg["analysis"]["position_bin"])),
    }
    n_pairs = summ["pair"].nunique() if len(summ) else 0
    headline = (f"Across {n_pairs} pair(s), treatment accounts' feeds carried "
                f"{_fmt(mean_did, pct=True)} more MH-themed items than their yoked controls during treatment, "
                f"relative to baseline (difference-in-differences)." if mean_did is not None else
                "Not enough sessions yet to estimate the treatment effect (needs baseline and treatment sessions for at least one pair).")
    measure_note = ("Gemini outcome labels (codebook v1)" if measure == "gemini" else
                    "keyword lists (no Gemini outcome labels yet: run label_items.py)")
    title = "Short-video drift audit: MVP report" + (" (PILOT DATA)" if args.dry_run else "")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Drift Audit Report</title>
<style>{BASE_CSS}</style></head><body><main>
<h1>{esc(title)}</h1>
<p class="muted">Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · YouTube Shorts · outcome measure: {esc(measure_note)}
· {labeled_share:.0%} of organic items labeled</p>

<div class="card"><p style="color:var(--ink);font-size:17px;margin:0">{esc(headline)}</p>
<p style="margin:8px 0 0">Exploratory: with {n_pairs} pair(s) the exact sign-flip p-value is {_fmt(p)}
(smallest attainable: {_fmt(2 / 2 ** n_pairs if n_pairs else None)}). This run shows the method yields a measurable
signal; it does not establish an effect. Scaling to more pairs is a change to accounts.yaml only.</p></div>

<h2>Design in one paragraph</h2>
<p>Each pair of fresh sock-puppet accounts runs side by side. In <strong>baseline</strong> and <strong>washout</strong>
both accounts follow the same content-blind dwell schedule. In the <strong>seed</strong> session the treatment account
watches a fixed list of sad/lonely Shorts and the control a duration-matched neutral list. In <strong>treatment</strong>
the treatment account lingers on mental-health-adjacent items (live Gemini classifier, keyword fallback) and the
control lingers exactly where its partner did (yoked), so watch time, timing, and network are matched and only the
<em>content</em> of what was lingered on differs. No account ever lingers on suicide/self-harm content; the bot never
likes, searches, or follows. Outcome: the share of items the feed served that carry a sadness or mental-health theme.</p>

<h2>Feed composition over the protocol</h2>
{_fig(figs['share'], 'Share of MH-themed items by session, treatment vs control')}
<h3>Treatment minus control, per pair</h3>
{_fig(figs['diff'], 'Treatment minus control share per pair by session')}
<h3>Harmful items (suicide / self-harm / pro-ED)</h3>
{_fig(figs['harm'], 'Share of harmful items by session', 'No feed item has been labeled harmful so far.')}
<h3>Within-session position (treatment phase)</h3>
{_fig(figs['pos'], 'Share MH-themed by position in session')}

<h2>Per-pair estimates</h2>
<p>DiD and persistence are in shares (0.10 = 10 percentage points). Drift session: first treatment session whose
difference exceeds the largest baseline difference by more than {margin}.</p>
{_table(summ)}

<h2>Measurement validity</h2>
{validation_html(cfg)}

<h2>Dose check (watch time per account and phase)</h2>
<p>Yoking should make long dwells and watch time near-identical within each pair; differences come from video
length, ads, and exclusions.</p>
{_table(dose_table(df), 1)}

<h2>Per-account, per-session shares</h2>
{_table(acct)}

<h2>Collection quality</h2>
<p><code>gemini_rate</code>: share of classified items decided by the model rather than the keyword fallback.
<code>active_video_rate</code>: polls where the active player was found (front-end health).</p>
{_table(quality_table(logs), 2)}

<h2>Limitations</h2>
<ul>
<li>{n_pairs} pair(s): descriptive only. All accounts share one IP and one machine, so the platform may link them
(this pushes results toward no difference).</li>
<li>Desktop web Shorts, not the mobile app; YouTube only.</li>
<li>The outcome is a classifier's judgment; see validity above. Removed videos are labeled from captured frames and metadata.</li>
<li>Full list: docs/threats_to_validity.md.</li>
</ul>
</main></body></html>"""
    out_dir = ROOT / cfg["paths"]["reports_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("mvp_report_dryrun.html" if args.dry_run else "mvp_report.html")
    out.write_text(html, encoding="utf-8")
    acct.to_csv(out_dir / ("account_sessions_dryrun.csv" if args.dry_run else "account_sessions.csv"), index=False)
    print(f"{headline}\nmeasure={measure}  labeled={labeled_share:.0%}  report -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args())
