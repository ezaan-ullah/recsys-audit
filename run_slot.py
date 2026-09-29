"""Run one session slot: every selected account scrolls one session.

Real protocol:
    python run_slot.py --phase baseline  --session 1
    python run_slot.py --phase treatment --session 3
    python run_slot.py --phase washout   --session 11

Dry run (feasibility check / calibrating neutral_long_prob), no groups needed:
    python run_slot.py --phase treatment --session 1 --dry-run --mode treatment \
        --accounts acct01,acct02 --n-items 15
"""
import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from audit.browser import open_profile
from audit.config import ROOT, load_accounts, load_config, policy_fingerprint
from audit.metadata import MetadataClient
from audit.policy import Policy
from audit.session import SessionAbort, run_session

PHASES = ("baseline", "treatment", "washout")


def mode_for(account: dict, phase: str, override: str | None) -> str:
    if override:
        return override
    return "treatment" if (phase == "treatment" and account["group"] == "treatment") else "neutral"


def interleave(accounts: list[dict], seed: int, session: int) -> list[dict]:
    """Alternate groups so treatment and control run side by side in time."""
    rng = random.Random(f"{seed}|order|{session}")
    t = [a for a in accounts if a.get("group") == "treatment"]
    c = [a for a in accounts if a.get("group") != "treatment"]
    rng.shuffle(t)
    rng.shuffle(c)
    out = []
    for i in range(max(len(t), len(c))):
        pair = [x[i] for x in (t, c) if i < len(x)]
        rng.shuffle(pair)
        out.extend(pair)
    return out


def summarize(path, run_id: str) -> dict:
    n = long = playing = meta_ok = 0
    if path.exists():
        for line in path.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("run_id") != run_id:
                continue
            n += 1
            long += bool(r["long"])
            playing += r["playing"] is True
            meta_ok += r["meta_source"] != "none"
    rate = (lambda k: round(k / n, 3) if n else None)
    return {"items": n, "long_rate": rate(long), "playing_rate": rate(playing), "meta_coverage": rate(meta_ok)}


async def main(args) -> None:
    cfg = load_config()
    accounts = load_accounts(cfg)
    if args.accounts:
        wanted = set(args.accounts.split(","))
        accounts = [a for a in accounts if a["id"] in wanted]
    if not accounts:
        sys.exit("No matching accounts.")
    if not args.dry_run and any(a.get("group") not in ("treatment", "control") for a in accounts):
        sys.exit("Some accounts have no group. Run assign_groups.py first (or use --dry-run).")

    api_key = os.environ.get(cfg["youtube"]["api_key_env"])
    if not api_key:
        print("WARNING: no YouTube API key; falling back to titles only (no tags, descriptions, "
              "or durations). Treatment triggering will be less accurate.")

    fp = policy_fingerprint(cfg)
    policy = Policy.from_config(cfg)
    meta = MetadataClient(api_key, ROOT / cfg["paths"]["meta_cache"])
    seed = cfg["study"]["seed"]
    n_items = args.n_items or cfg["study"]["items_per_session"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / cfg["paths"]["data_dir"] / ("dryrun" if args.dry_run else "raw") / f"{args.phase}_s{args.session:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.dry_run and not args.force:
        existing = [a["id"] for a in accounts if (out_dir / f"{a['id']}.jsonl").exists()]
        if existing:
            sys.exit(f"Data already exists for {existing} in this slot. Use --force to re-run "
                     "(e.g. after a crash) and note it in your lab log.")

    print(f"run_id={run_id}  phase={args.phase}  session={args.session}  policy_fp={fp}  items={n_items}")
    sem = asyncio.Semaphore(cfg["study"]["concurrency"])

    async with async_playwright() as pw:

        async def one(a: dict) -> dict:
            async with sem:
                mode = mode_for(a, args.phase, args.mode)
                out_path = out_dir / f"{a['id']}.jsonl"
                rng = random.Random(f"{seed}|{a['id']}|{args.phase}|{args.session}")
                started = datetime.now(timezone.utc).isoformat(timespec="seconds")
                status, error, ctx = "ok", None, None
                try:
                    ctx = await open_profile(pw, cfg["browser"], a["id"])
                    await run_session(ctx, account_id=a["id"], group=a.get("group"), phase=args.phase,
                                      mode=mode, session_no=args.session, n_items=n_items, policy=policy,
                                      rng=rng, meta=meta, out_path=out_path, fingerprint=fp, run_id=run_id)
                except SessionAbort as e:
                    status, error = e.status, str(e)
                except Exception as e:  # keep the other accounts running
                    status, error = "error", f"{type(e).__name__}: {e}"
                finally:
                    if ctx is not None:
                        try:
                            await ctx.close()
                        except Exception:
                            pass
                rec = {"run_id": run_id, "account_id": a["id"], "group": a.get("group"), "mode": mode,
                       "phase": args.phase, "session": args.session, "started_utc": started,
                       "ended_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "status": status, "error": error, "policy_fp": fp, **summarize(out_path, run_id)}
                with open(out_dir / "_session_log.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"{a['id']:>8} {mode:<9} {status:<13} items={rec['items']:<3} long={rec['long_rate']} "
                      f"playing={rec['playing_rate']} meta={rec['meta_coverage']}" + (f"  ({error})" if error else ""))
                return rec

        results = await asyncio.gather(*(one(a) for a in interleave(accounts, seed, args.session)))

    await meta.close()

    treat = [r["long_rate"] for r in results if r["mode"] == "treatment" and r["long_rate"] is not None]
    if treat:
        print(f"\nMean long-dwell rate in treatment mode: {sum(treat) / len(treat):.3f} "
              f"(candidate value for policy.neutral_long_prob)")
    failed = [r["account_id"] for r in results if r["status"] != "ok"]
    if failed:
        print(f"Accounts needing attention: {failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=PHASES)
    ap.add_argument("--session", required=True, type=int)
    ap.add_argument("--accounts", help="comma-separated subset, e.g. acct01,acct02")
    ap.add_argument("--n-items", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mode", choices=("treatment", "neutral"), help="override (dry runs only)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.mode and not args.dry_run:
        ap.error("--mode is only allowed with --dry-run")
    asyncio.run(main(args))
