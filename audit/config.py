"""Config, accounts, protocol, and the protocol fingerprint."""
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Source files whose behavior is part of the pre-registered procedure.
FINGERPRINT_CODE = ("audit/policy.py", "audit/session.py", "audit/classifier.py",
                    "audit/gemini.py", "audit/dom.py", "audit/yoke.py")


def load_config(path: str = "config.yaml") -> dict:
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_accounts(cfg: dict) -> list[dict]:
    with open(ROOT / cfg["paths"]["accounts"], encoding="utf-8") as f:
        return yaml.safe_load(f).get("accounts") or []


def save_accounts(cfg: dict, accounts: list[dict]) -> None:
    path = ROOT / cfg["paths"]["accounts"]
    header = []
    if path.exists():   # keep the explanatory comment block at the top of the file
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("#"):
                break
            header.append(line)
    body = yaml.safe_dump({"accounts": accounts}, sort_keys=False, allow_unicode=True,
                          default_flow_style=None, width=200)
    path.write_text("\n".join(header + [body]), encoding="utf-8")


def load_protocol(cfg: dict) -> dict:
    with open(ROOT / cfg["paths"]["protocol"], encoding="utf-8") as f:
        proto = yaml.safe_load(f)
    nums = [s["n"] for s in proto["sessions"]]
    if nums != list(range(1, len(nums) + 1)):
        raise ValueError("protocol.yaml sessions must be numbered 1..N in order")
    return proto


def session_spec(proto: dict, n: int) -> dict:
    for s in proto["sessions"]:
        if s["n"] == n:
            return s
    raise KeyError(f"session {n} is not in protocol.yaml")


def load_terms(path: Path) -> list[str]:
    """One term per line; '#' at line start is a comment. Lowercased, whitespace collapsed."""
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            terms.append(re.sub(r"\s+", " ", s.lower()))
    return terms


def load_ids(path: Path) -> list[str]:
    """Video IDs, one per line; anything after the ID (or a '#' line) is ignored."""
    if not path.exists():
        return []
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            ids.append(s.split()[0])
    return ids


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_inputs(cfg: dict) -> dict:
    """Everything that defines the procedure. Parsed, not raw, where comments could differ."""
    ccfg = cfg["classifier"]
    return {
        "study": {k: cfg["study"][k] for k in ("seed", "items_per_session")},
        "policy": cfg["policy"],
        "yoke": cfg["yoke"],
        "protocol": load_protocol(cfg),
        "keywords": {k: sorted(set(load_terms(ROOT / p))) for k, p in sorted(cfg["keywords"].items())},
        "seeds": {k: load_ids(ROOT / p) for k, p in sorted(cfg["seeds"].items())},
        "classifier": {
            "enabled": ccfg["enabled"], "model": ccfg["model"], "thinking_budget": ccfg.get("thinking_budget"),
            "prompt": _sha((ROOT / ccfg["prompt"]).read_bytes()),
        },
        "code": {p: _sha((ROOT / p).read_bytes()) for p in FINGERPRINT_CODE if (ROOT / p).exists()},
        "lock": _sha((ROOT / "requirements.lock").read_bytes()) if (ROOT / "requirements.lock").exists() else None,
    }


def protocol_fingerprint(cfg: dict) -> str:
    """Hash of the full procedure, logged with every row.

    Pre-register this value (config.yaml: preregistered_fp). Real runs refuse to
    start if the procedure has changed since.
    """
    blob = json.dumps(fingerprint_inputs(cfg), sort_keys=True, ensure_ascii=False).encode()
    return _sha(blob)[:12]


# Kept for older callers; the fingerprint now covers the whole procedure.
policy_fingerprint = protocol_fingerprint


def run_environment() -> dict:
    """Software versions and code revision, recorded in every session-log row."""
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                                  timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    def version(pkg):
        try:
            return importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            return None

    status = git("status", "--porcelain", "--untracked-files=no")
    return {"git_commit": git("rev-parse", "--short", "HEAD"), "git_dirty": bool(status),
            "python": platform.python_version(), "playwright": version("playwright"),
            "google_genai": version("google-genai")}
