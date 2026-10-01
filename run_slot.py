"""Run one session slot: every selected account runs session N of protocol.yaml.

Real protocol (study accounts; the phase comes from protocol.yaml):
    python run_slot.py --session 1

Re-run after a crash (both members of the pair, with a reason for the lab log):
    python run_slot.py --session 5 --force --accounts acct03,acct04 --reason "Chrome crashed"

Pilot / dry run (pilot accounts only; phase and length may be overridden):
    python run_slot.py --session 4 --dry-run --accounts acct01,acct02 --n-items 20
"""
import argparse
import asyncio
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from audit.browser import open_profile
from audit.classifier import TriggerClassifier
from audit.config import (ROOT, load_accounts, load_config, load_ids, load_protocol, protocol_fingerprint,
                          run_environment, session_spec)
from audit.errors import SessionAbort
from audit.gemini import GeminiClient, RateLimiter
from audit.media import MediaStore
from audit.metadata import MetadataClient
from audit.policy import Keywords, Policy
from audit.session import Runtime, SessionSpec, run_session
from audit.yoke import YokeLink

PHASES = ("baseline", "seed", "treatment", "washout")
# (treatment arm mode, control arm mode) per phase
MODES = {"baseline": ("neutral", "neutral"), "washout": ("neutral", "neutral"),
         "seed": ("seed", "seed"), "treatment": ("treatment", "yoked")}
MODEL_SOURCES = ("gemini", "cache")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slot_dir(cfg: dict, dry_run: bool, phase: str, session: int):
    return ROOT / cfg["paths"]["data_dir"] / ("dryrun" if dry_run else "raw") / f"{phase}_s{session:02d}"


def read_log(path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def latest_by_account(log: list[dict]) -> dict[str, dict]:
    latest = {}
    for r in log:
        latest[r["account_id"]] = r          # the log is append-only, so the last row wins
    return latest


def form_pairs(accounts: list[dict]) -> dict[str, dict]:
    """{pair_id: {"treatment": acct, "control": acct}}; raises ValueError if any pair is incomplete."""
    pairs: dict[str, dict] = defaultdict(dict)
    for a in accounts:
        if a.get("group") not in ("treatment", "control") or not a.get("pair"):
            raise ValueError(f"{a['id']} has no pair/group. Run assign_groups.py first.")
        if a["group"] in pairs[a["pair"]]:
            raise ValueError(f"pair {a['pair']} has two {a['group']} accounts")
        pairs[a["pair"]][a["group"]] = a
    for pid, p in pairs.items():
        if set(p) != {"treatment", "control"}:
            raise ValueError(f"pair {pid} is incomplete in this selection; run both members together")
    return dict(pairs)


def protocol_issues(cfg: dict, proto: dict, session: int, ids: list[str], now: datetime) -> list[str]:
    """Order and spacing problems for a real run of `session`."""
    if session == 1:
        return []
    prev = session_spec(proto, session - 1)
    log = latest_by_account(read_log(slot_dir(cfg, False, prev["phase"], prev["n"]) / "_session_log.jsonl"))
    issues = []
    for aid in ids:
        r = log.get(aid)
        if r is None:
            issues.append(f"{aid}: session {prev['n']} was never run")
        elif r["status"] != "ok":
            issues.append(f"{aid}: session {prev['n']} ended with status {r['status']}")
    ends = [datetime.fromisoformat(log[a]["ended_utc"]) for a in ids if a in log]
    if ends:
        gap_h = (now - max(ends)).total_seconds() / 3600
        if gap_h < float(proto.get("min_gap_hours", 0)):
            issues.append(f"only {gap_h:.1f} h since session {prev['n']} (min_gap_hours={proto['min_gap_hours']})")
    return issues


def summarize(path, run_id: str) -> dict:
    rows = [r for r in read_log(path) if r.get("run_id") == run_id]
    org = [r for r in rows if not r.get("is_ad")]
    classified = [r for r in org if r.get("trigger_source")]
    framed = [r for r in rows if r.get("frames_saved") is not None]
    rate = lambda k, n: round(k / n, 3) if n else None
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    return {
        "items": len(org), "ads": len(rows) - len(org),
        "long_rate": rate(sum(bool(r["long"]) for r in org), len(org)),
        "excluded": sum(r["reason"] == "excluded" for r in org),
        "classified": len(classified),
        "gemini_rate": rate(sum(r["trigger_source"] in MODEL_SOURCES for r in classified), len(classified)),
        "data_api_rate": rate(sum(r.get("meta_source") == "data_api" for r in rows), len(rows)),
        "oembed_rate": rate(sum(r.get("meta_source") == "oembed" for r in rows), len(rows)),
        "active_video_rate": mean([r["active_video_frac"] for r in rows if r.get("active_video_frac") is not None]),
        "playing_rate": mean([r["playing_frac"] for r in rows if r.get("playing_frac") is not None]),
        "start_ok_rate": rate(sum(bool(r.get("playing_started")) for r in rows), len(rows)),
        "muted_rate": rate(sum(r.get("muted") is True for r in rows), len(rows)),
        "frames_rate": rate(sum((r["frames_saved"] or 0) > 0 for r in framed), len(framed)),
        "interstitials": sum(bool(r.get("interstitial")) for r in rows),
        "watch_s": round(sum(r.get("dwell_s") or 0 for r in rows), 1),
        "long_watch_s": round(sum(r.get("dwell_s") or 0 for r in org if r["long"]), 1),
        "yoke_wait_s": round(sum(r.get("yoke_wait_s") or 0 for r in rows), 1),
    }


def quality_flags(s: dict, mode: str, min_gemini_rate: float) -> list[str]:
    flags = []
    if mode == "treatment" and s["classified"] and (s["gemini_rate"] or 0) < min_gemini_rate:
        flags.append(f"gemini_rate<{min_gemini_rate}")
    if s["active_video_rate"] is not None and s["active_video_rate"] < 0.95:
        flags.append("active_video<0.95")
    if s["muted_rate"]:
        flags.append("player_muted")
    if s["start_ok_rate"] is not None and s["start_ok_rate"] < 0.9:
        flags.append("playback_start<0.9")
    return flags


def build_classifier(cfg: dict, media: MediaStore, require: bool) -> TriggerClassifier:
    ccfg = cfg["classifier"]
    key = os.environ.get(ccfg["api_key_env"])
    gemini = None
    if ccfg["enabled"] and ccfg.get("model") and key:
        limiter = RateLimiter(ccfg["rpm"], ccfg["rpd"], ROOT / cfg["paths"]["gemini_usage"])
        gemini = GeminiClient(key, ccfg["model"], ccfg.get("thinking_budget"), limiter)
    elif ccfg["enabled"]:
        msg = (f"classifier.enabled but {'classifier.model is not set' if not ccfg.get('model') else ccfg['api_key_env'] + ' is not set'}; "
               "the trigger would fall back to keywords on every item.")
        if require:
            sys.exit(msg)
        print("WARNING: " + msg)
    prompt = (ROOT / ccfg["prompt"]).read_text(encoding="utf-8")
    return TriggerClassifier(Keywords.from_config(cfg), gemini, prompt, ROOT / cfg["paths"]["trigger_cache"], media)


def supersede(out_dir, account_id: str, attempt: int) -> None:
    """Move a previous attempt's rows aside so the slot holds exactly one attempt per account."""
    src = out_dir / f"{account_id}.jsonl"
    if src.exists():
        dst = out_dir / "_superseded"
        dst.mkdir(exist_ok=True)
        src.rename(dst / f"{account_id}.attempt{attempt - 1}.jsonl")


def append_jsonl(path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def main(args) -> None:
    cfg = load_config()
    proto = load_protocol(cfg)
    accounts = load_accounts(cfg)
    role = "pilot" if args.dry_run else "study"

    by_id = {a["id"]: a for a in accounts}
    if args.accounts:
        wanted = args.accounts.split(",")
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            sys.exit(f"Unknown accounts: {unknown}")
        wrong = [w for w in wanted if by_id[w].get("role") != role]
        if wrong:
            sys.exit(f"{wrong} are not {role} accounts. Dry runs use pilot accounts only; real runs use study accounts only.")
        selected = [by_id[w] for w in wanted]
    else:
        selected = [a for a in accounts if a.get("role") == role]
    if not selected:
        sys.exit(f"No {role} accounts selected. Add them to accounts.yaml.")
    try:
        pairs = form_pairs(selected)
    except ValueError as e:
        sys.exit(str(e))

    try:
        spec_n = session_spec(proto, args.session)
    except KeyError as e:
        sys.exit(str(e))
    phase = args.phase if (args.dry_run and args.phase) else spec_n["phase"]

    fp = protocol_fingerprint(cfg)
    if not args.dry_run and cfg.get("preregistered_fp") != fp:
        sys.exit(f"Procedure fingerprint is {fp}, but config.yaml preregistered_fp is {cfg.get('preregistered_fp')}.\n"
                 "Either the procedure changed since pre-registration, or you have not frozen it yet.")
    n_items = args.n_items or cfg["study"]["items_per_session"]

    seeds = {}
    if phase == "seed":
        seeds = {g: tuple(load_ids(ROOT / cfg["seeds"][g])) for g in ("treatment", "control")}
        if not seeds["treatment"] or len(seeds["treatment"]) != len(seeds["control"]):
            sys.exit("Seed lists must be non-empty and the same length (seeds/treatment.txt, seeds/control.txt).")

    out_dir = slot_dir(cfg, args.dry_run, phase, args.session)
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = [a["id"] for a in selected]

    if not args.dry_run:
        problems = protocol_issues(cfg, proto, args.session, ids, datetime.now(timezone.utc))
        existing = [i for i in ids if (out_dir / f"{i}.jsonl").exists()]
        if existing:
            problems.append(f"data already exists for {existing} in this slot")
        if problems and not args.force:
            sys.exit("Refusing to run:\n  - " + "\n  - ".join(problems) +
                     "\nIf this is intended, re-run with --force --accounts <both pair members> --reason \"...\".")
        if args.force and (not args.accounts or not args.reason):
            sys.exit("--force needs --accounts (whole pairs) and --reason, so the deviation is logged.")

    log_path = out_dir / "_session_log.jsonl"
    prior = defaultdict(int)
    for r in read_log(log_path):
        prior[r["account_id"]] += 1
    attempts = {i: prior[i] + 1 for i in ids}
    for i in ids:
        supersede(out_dir, i, attempts[i])

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    env = run_environment()
    append_jsonl(ROOT / cfg["paths"]["lab_log"], {
        "utc": _utc_now(), "run_id": run_id, "session": args.session, "phase": phase, "dry_run": args.dry_run,
        "accounts": ids, "force": args.force, "reason": args.reason, "policy_fp": fp, "n_items": n_items, **env})

    seed = cfg["study"]["seed"]
    policy = Policy.from_config(cfg)
    yt_key = os.environ.get(cfg["youtube"]["api_key_env"])
    if not yt_key:
        print("WARNING: no YouTube Data API key; metadata falls back to oEmbed (title and channel only).")
    meta = MetadataClient(yt_key, ROOT / cfg["paths"]["meta_cache"])
    media = MediaStore(ROOT / cfg["paths"]["media_dir"])
    classifier = build_classifier(cfg, media, require=not args.dry_run)

    max_pairs = int(cfg["study"]["max_pairs_in_flight"])
    if phase == "treatment" and classifier.gemini is not None:
        cap = int(cfg["classifier"]["rpm"]) // int(cfg["classifier"]["per_treatment_account_rpm"])
        max_pairs = max(1, min(max_pairs, cap))
    sem = asyncio.Semaphore(max_pairs)
    print(f"run_id={run_id}  session={args.session}  phase={phase}  policy_fp={fp}  items={n_items}  "
          f"pairs={len(pairs)} ({max_pairs} at a time)")

    order = sorted(pairs)
    random.Random(f"{seed}|order|{args.session}").shuffle(order)

    async with async_playwright() as pw:

        async def run_account(a: dict, mode: str, link: YokeLink | None, delay_s: float, pair_seed: str) -> dict:
            out_path = out_dir / f"{a['id']}.jsonl"
            started = _utc_now()
            status, error, ctx, info = "ok", None, None, {}
            try:
                if delay_s:
                    await asyncio.sleep(delay_s)
                if mode == "yoked" and link.closed and not link.published:
                    # Partner ended with no decisions; otherwise the control still replays what it did publish.
                    raise SessionAbort("partner_aborted", f"treatment partner ended ({link.closed}) before start")
                ctx = await open_profile(pw, cfg["browser"], a["id"])
                spec = SessionSpec(
                    account_id=a["id"], group=a["group"], pair=a["pair"], phase=phase, mode=mode,
                    session_no=args.session, attempt=attempts[a["id"]], n_items=n_items, run_id=run_id,
                    fingerprint=fp, out_path=out_path, expected_datasync=a.get("datasync_id"),
                    require_identity=not args.dry_run, seed_ids=seeds.get(a["group"], ()))
                rt = Runtime(policy=policy, pair_rng=random.Random(pair_seed),
                             ad_rng=random.Random(f"{seed}|{a['id']}|ads|{args.session}|{attempts[a['id']]}"),
                             meta=meta, classifier=classifier, media=media, yoke=link,
                             partner_timeout_s=float(cfg["yoke"]["partner_timeout_s"]),
                             check_history=bool(cfg["checks"]["history_page"]))
                await run_session(ctx, spec, rt, info)
            except SessionAbort as e:
                status, error = e.status, str(e)
            except asyncio.CancelledError:
                status, error = "interrupted", "cancelled (Ctrl-C or shutdown)"
                raise
            except Exception as e:  # keep the other accounts running
                status, error = "error", f"{type(e).__name__}: {e}"
            finally:
                if mode == "treatment" and link is not None:
                    link.close(status)
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                summary = summarize(out_path, run_id)
                rec = {"run_id": run_id, "account_id": a["id"], "group": a["group"], "pair": a["pair"],
                       "mode": mode, "phase": phase, "session": args.session, "attempt": attempts[a["id"]],
                       "dry_run": args.dry_run, "started_utc": started, "ended_utc": _utc_now(),
                       "status": status, "error": error, "policy_fp": fp, "n_items": n_items,
                       "reason": args.reason, **summary,
                       "flags": quality_flags(summary, mode, float(cfg["classifier"]["min_gemini_rate"])),
                       **env, **{k: info.get(k) for k in ("chrome_version", "datasync_id", "hl", "gl")}}
                append_jsonl(log_path, rec)
                print(f"{a['id']:>8} {mode:<9} {status:<15} items={rec['items']:<3} ads={rec['ads']:<2} "
                      f"long={rec['long_rate']} gemini={rec['gemini_rate']} video={rec['active_video_rate']} "
                      f"frames={rec['frames_rate']} {' '.join(rec['flags'])}" + (f"  ({error})" if error else ""))
            return rec

        async def run_pair(pid: str) -> list[dict]:
            async with sem:
                t, c = pairs[pid]["treatment"], pairs[pid]["control"]
                mode_t, mode_c = MODES[phase]
                link = YokeLink() if phase == "treatment" else None
                attempt = max(attempts[t["id"]], attempts[c["id"]])
                pair_seed = f"{seed}|{pid}|{args.session}|{attempt}"
                delay = float(cfg["yoke"]["control_start_delay_s"]) if link else 0.0
                return await asyncio.gather(run_account(t, mode_t, link, 0.0, pair_seed),
                                            run_account(c, mode_c, link, delay, pair_seed))

        results = [r for pr in await asyncio.gather(*(run_pair(p) for p in order)) for r in pr]

    await meta.close()
    await media.close()

    failed = [r["account_id"] for r in results if r["status"] != "ok"]
    flagged = [r["account_id"] for r in results if r["flags"]]
    if failed:
        print(f"\nAccounts needing attention: {failed}  (re-run whole pairs with --force --accounts ... --reason ...)")
    if flagged:
        print(f"Quality flags on: {flagged}  (see {log_path.relative_to(ROOT)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, type=int)
    ap.add_argument("--accounts", help="comma-separated subset, e.g. acct03,acct04 (whole pairs)")
    ap.add_argument("--dry-run", action="store_true", help="pilot accounts only; writes to data/dryrun")
    ap.add_argument("--phase", choices=PHASES, help="override the protocol phase (dry runs only)")
    ap.add_argument("--n-items", type=int, help="override items_per_session (dry runs only)")
    ap.add_argument("--force", action="store_true", help="re-run or run out of order (logged)")
    ap.add_argument("--reason", help="why --force was needed; written to the lab log")
    args = ap.parse_args()
    if (args.phase or args.n_items) and not args.dry_run:
        ap.error("--phase and --n-items are only allowed with --dry-run")
    asyncio.run(main(args))
