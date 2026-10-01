"""Which sessions has each study account completed? One row per account, one column per session.

    python protocol_status.py            # real runs (data/raw)
    python protocol_status.py --dry-run  # pilot runs (data/dryrun)

Cell: ok / status of the latest attempt, '·' if never run; '*' marks a re-run, '!' quality flags.
"""
import argparse

from audit.config import load_accounts, load_config, load_protocol
from run_slot import latest_by_account, read_log, slot_dir

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

cfg = load_config()
proto = load_protocol(cfg)
role = "pilot" if args.dry_run else "study"
accounts = [a for a in load_accounts(cfg) if a.get("role") == role]

cols = proto["sessions"]
print(f"{'account':<8} {'pair':<6} {'group':<10} " + " ".join(f"{s['phase'][:4]}{s['n']:<3}" for s in cols))
problems = []
for a in accounts:
    cells = []
    for s in cols:
        log = latest_by_account(read_log(slot_dir(cfg, args.dry_run, s["phase"], s["n"]) / "_session_log.jsonl"))
        r = log.get(a["id"])
        if r is None:
            cells.append(f"{'·':<7}")
            continue
        mark = ("*" if r.get("attempt", 1) > 1 else "") + ("!" if r.get("flags") else "")
        cells.append(f"{(r['status'][:5] + mark):<7}")
        if r["status"] != "ok" or r.get("flags"):
            problems.append(f"{a['id']} s{s['n']}: {r['status']} {r.get('flags') or ''} {r.get('error') or ''}".rstrip())
    print(f"{a['id']:<8} {a.get('pair')!s:<6} {a.get('group')!s:<10} " + " ".join(cells))
if problems:
    print("\n" + "\n".join(problems))
