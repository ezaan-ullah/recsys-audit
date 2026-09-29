"""Config and account loading, plus the policy fingerprint."""
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str = "config.yaml") -> dict:
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_accounts(cfg: dict) -> list[dict]:
    with open(ROOT / cfg["paths"]["accounts"], encoding="utf-8") as f:
        return yaml.safe_load(f)["accounts"]


def save_accounts(cfg: dict, accounts: list[dict]) -> None:
    with open(ROOT / cfg["paths"]["accounts"], "w", encoding="utf-8") as f:
        yaml.safe_dump({"accounts": accounts}, f, sort_keys=False, allow_unicode=True)


def policy_fingerprint(cfg: dict) -> str:
    """Hash of everything that defines the treatment policy.

    Logged with every row, so you can show (and pre-register) that the
    treatment rule never changed during collection.
    """
    h = hashlib.sha256()
    h.update(json.dumps(cfg["policy"], sort_keys=True).encode())
    for key in ("adjacent", "exclude"):
        h.update((ROOT / cfg["keywords"][key]).read_bytes())
    return h.hexdigest()[:12]
