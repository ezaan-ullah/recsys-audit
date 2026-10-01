"""Form pairs of study accounts and randomize treatment/control within each pair (seeded).

Run once, after every study account exists and is signed in. Accounts are shuffled, then
ordered by declared persona (gender, birth year) so each pair is as alike as possible;
which member gets treatment is a coin flip per pair.
"""
import argparse
import random

from audit.config import load_accounts, load_config, save_accounts

ap = argparse.ArgumentParser()
ap.add_argument("--force", action="store_true", help="overwrite an existing assignment")
args = ap.parse_args()

cfg = load_config()
accounts = load_accounts(cfg)
study = [a for a in accounts if a.get("role") == "study"]
if not study:
    raise SystemExit("No study accounts in accounts.yaml.")
if len(study) % 2:
    raise SystemExit(f"{len(study)} study accounts: pairs need an even number.")
if any(a.get("group") or a.get("pair") for a in study) and not args.force:
    raise SystemExit("Groups already assigned. Use --force only if no real data has been collected.")

rng = random.Random(f"{cfg['study']['seed']}|assign")
order = list(study)
rng.shuffle(order)
order.sort(key=lambda a: (str(a.get("gender")), a.get("birth_year") or 0))   # stable: ties keep shuffled order
for i in range(0, len(order), 2):
    first, second = order[i], order[i + 1]
    t, c = (first, second) if rng.random() < 0.5 else (second, first)
    pid = f"p{i // 2 + 1}"
    t.update(pair=pid, group="treatment")
    c.update(pair=pid, group="control")
save_accounts(cfg, accounts)

print(f"{'pair':<5} {'treatment':<10} {'control':<10} persona (treatment | control)")
for pid in sorted({a["pair"] for a in study}, key=lambda p: int(p[1:])):
    t = next(a for a in study if a["pair"] == pid and a["group"] == "treatment")
    c = next(a for a in study if a["pair"] == pid and a["group"] == "control")
    persona = lambda a: f"{a.get('gender')}/{a.get('birth_year')}/{a.get('created')}"
    print(f"{pid:<5} {t['id']:<10} {c['id']:<10} {persona(t)} | {persona(c)}")
print(f"seed={cfg['study']['seed']}  pairs={len(study) // 2}")
