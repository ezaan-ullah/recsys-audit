"""run_slot.main against a temporary repo, with the browser and session faked out."""
import argparse
import asyncio
import json
import shutil
from contextlib import asynccontextmanager

import pytest

import audit.config as config
import run_slot
from audit.errors import SessionAbort

STUDY = """accounts:
- {id: acct01, role: pilot, pair: pilot, group: treatment, datasync_id: null}
- {id: acct02, role: pilot, pair: pilot, group: control, datasync_id: null}
- {id: acct03, role: study, pair: p1, group: treatment, datasync_id: D3}
- {id: acct04, role: study, pair: p1, group: control, datasync_id: D4}
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    src = config.ROOT
    for p in ("config.yaml", "protocol.yaml", "keywords", "seeds", "prompts", "audit"):
        s = src / p
        (shutil.copytree if s.is_dir() else shutil.copy)(s, tmp_path / p)
    (tmp_path / "accounts.yaml").write_text(STUDY)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(run_slot, "ROOT", tmp_path)

    @asynccontextmanager
    async def fake_pw():
        yield None

    class Ctx:
        async def close(self):
            pass

    async def fake_open(pw, bcfg, account_id, screen=None):
        return Ctx()

    monkeypatch.setattr(run_slot, "async_playwright", fake_pw)
    monkeypatch.setattr(run_slot, "open_profile", fake_open)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("YT_API_KEY", raising=False)
    return tmp_path


def args(**kw):
    base = dict(session=4, accounts=None, dry_run=True, phase=None, n_items=3, force=False, reason=None)
    base.update(kw)
    return argparse.Namespace(**base)


def log_rows(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def write_row(spec, i=0):
    with open(spec.out_path, "a") as f:
        f.write(json.dumps({"run_id": spec.run_id, "is_ad": False, "long": False, "reason": "neutral_skip",
                            "trigger_source": None, "dwell_s": 4.0, "position": i}) + "\n")


def test_partner_abort_propagates_and_every_account_is_logged(repo, monkeypatch):
    async def fake_session(ctx, spec, rt, info):
        if spec.mode == "treatment":
            write_row(spec)
            rt.yoke.publish(0, False)
            raise SessionAbort("stuck", "could not advance")
        await rt.yoke.receive(0, 5)
        write_row(spec)
        await rt.yoke.receive(1, 5)          # partner never publishes item 1

    monkeypatch.setattr(run_slot, "run_session", fake_session)
    cfg = config.load_config()
    cfg["yoke"]["control_start_delay_s"] = 0
    monkeypatch.setattr(run_slot, "load_config", lambda: cfg)
    asyncio.run(run_slot.main(args()))
    log = {r["account_id"]: r for r in log_rows(repo / "data/dryrun/treatment_s04/_session_log.jsonl")}
    assert log["acct01"]["status"] == "stuck" and log["acct01"]["mode"] == "treatment"
    assert log["acct02"]["status"] == "partner_aborted" and log["acct02"]["mode"] == "yoked"
    assert log["acct02"]["items"] == 1


def test_cancellation_still_writes_the_session_log(repo, monkeypatch):
    async def fake_session(ctx, spec, rt, info):
        write_row(spec)
        raise asyncio.CancelledError()

    monkeypatch.setattr(run_slot, "run_session", fake_session)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_slot.main(args(phase="baseline")))
    log = log_rows(repo / "data/dryrun/baseline_s04/_session_log.jsonl")
    assert {r["status"] for r in log} == {"interrupted"} and {r["items"] for r in log} == {1}


def test_rerun_supersedes_previous_attempt(repo, monkeypatch):
    async def fake_session(ctx, spec, rt, info):
        write_row(spec)

    monkeypatch.setattr(run_slot, "run_session", fake_session)
    asyncio.run(run_slot.main(args(phase="baseline")))
    asyncio.run(run_slot.main(args(phase="baseline")))
    slot = repo / "data/dryrun/baseline_s04"
    assert len((slot / "acct01.jsonl").read_text().splitlines()) == 1
    assert (slot / "_superseded/acct01.attempt1.jsonl").exists()
    assert [r["attempt"] for r in log_rows(slot / "_session_log.jsonl") if r["account_id"] == "acct01"] == [1, 2]


def test_role_separation(repo):
    with pytest.raises(SystemExit, match="not pilot accounts"):
        asyncio.run(run_slot.main(args(accounts="acct03,acct04")))
    with pytest.raises(SystemExit, match="incomplete"):
        asyncio.run(run_slot.main(args(accounts="acct01")))


def test_real_run_requires_preregistered_fingerprint(repo):
    with pytest.raises(SystemExit, match="preregistered_fp"):
        asyncio.run(run_slot.main(args(dry_run=False, n_items=None, session=1)))


def test_protocol_order_gap_and_force_rules(repo, monkeypatch):
    cfg = config.load_config()
    proto = config.load_protocol(cfg)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    issues = run_slot.protocol_issues(cfg, proto, 2, ["acct03", "acct04"], now)
    assert any("never run" in i for i in issues)
    slot = run_slot.slot_dir(cfg, False, "baseline", 1)
    slot.mkdir(parents=True)
    ended = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    with open(slot / "_session_log.jsonl", "w") as f:
        for a, st in (("acct03", "ok"), ("acct04", "stuck")):
            f.write(json.dumps({"account_id": a, "status": st, "ended_utc": ended}) + "\n")
    issues = run_slot.protocol_issues(cfg, proto, 2, ["acct03", "acct04"], now)
    assert any("acct04" in i and "stuck" in i for i in issues)
    assert any("min_gap_hours" in i for i in issues)

    fp = config.protocol_fingerprint(cfg)
    cfg["preregistered_fp"] = fp
    monkeypatch.setattr(run_slot, "load_config", lambda: cfg)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cfg["classifier"]["model"] = "m"
    monkeypatch.setattr(run_slot, "protocol_fingerprint", lambda c: fp)
    with pytest.raises(SystemExit, match="Refusing to run"):
        asyncio.run(run_slot.main(args(dry_run=False, n_items=None, session=2)))
    with pytest.raises(SystemExit, match="--reason"):
        asyncio.run(run_slot.main(args(dry_run=False, n_items=None, session=2, force=True)))


def test_form_pairs_and_quality_flags():
    a = [{"id": "x", "pair": "p1", "group": "treatment"}, {"id": "y", "pair": "p1", "group": "treatment"}]
    with pytest.raises(ValueError, match="two treatment"):
        run_slot.form_pairs(a)
    s = {"classified": 10, "gemini_rate": 0.5, "active_video_rate": 0.9, "muted_rate": 0.0, "start_ok_rate": 1.0}
    assert run_slot.quality_flags(s, "treatment", 0.9) == ["gemini_rate<0.9", "active_video<0.95"]
    assert run_slot.quality_flags(s, "neutral", 0.9) == ["active_video<0.95"]
