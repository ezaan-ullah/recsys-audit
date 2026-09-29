"""Randomly assign accounts to treatment/control (seeded, balanced). Run once,
after every account exists and is signed in."""
import argparse
import random

from audit.config import load_accounts, load_config, save_accounts

ap = argparse.ArgumentParser()
ap.add_argument("--force", action="store_true", help="overwrite an existing assignment")
args = ap.parse_args()

cfg = load_config()
accounts = load_accounts(cfg)
if any(a.get("group") for a in accounts) and not args.force:
    raise SystemExit("Groups already assigned. Use --force only if no real data has been collected.")

ids = [a["id"] for a in accounts]
random.Random(cfg["study"]["seed"]).shuffle(ids)
treatment = set(ids[: len(ids) // 2])
for a in accounts:
    a["group"] = "treatment" if a["id"] in treatment else "control"
save_accounts(cfg, accounts)

for a in accounts:
    print(f"{a['id']:>8}  {a['group']}")
print(f"seed={cfg['study']['seed']}  treatment={len(treatment)}  control={len(ids) - len(treatment)}")
