import shutil

import pytest

import audit.config as config


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A copy of the repo's config-relevant files, with ROOT pointed at it."""
    src = config.ROOT
    for p in ("config.yaml", "protocol.yaml", "accounts.yaml", "keywords", "seeds", "prompts", "audit"):
        s = src / p
        (shutil.copytree if s.is_dir() else shutil.copy)(s, tmp_path / p)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_path


def test_fingerprint_ignores_keyword_comments(repo):
    cfg = config.load_config()
    fp = config.protocol_fingerprint(cfg)
    with open(repo / "keywords/adjacent.txt", "a", encoding="utf-8") as f:
        f.write("\n# a new comment\n\n")
    assert config.protocol_fingerprint(cfg) == fp


def test_fingerprint_changes_with_terms_prompt_code_seeds_and_protocol(repo):
    cfg = config.load_config()
    fp = config.protocol_fingerprint(cfg)
    edits = [("keywords/exclude.txt", "\nnewterm\n"), ("prompts/trigger_v1.md", "\nextra rule\n"),
             ("audit/session.py", "\n# changed\n"), ("seeds/treatment.txt", "\nabcdefghijk\n"),
             ("protocol.yaml", "\n# comment only\n")]
    seen = {fp}
    for path, text in edits:
        with open(repo / path, "a", encoding="utf-8") as f:
            f.write(text)
        new = config.protocol_fingerprint(cfg)
        if path == "protocol.yaml":
            assert new in seen          # parsed YAML: comments do not matter
        else:
            assert new not in seen
            seen.add(new)


def test_fingerprint_changes_with_policy_and_model(repo):
    cfg = config.load_config()
    fp = config.protocol_fingerprint(cfg)
    assert config.protocol_fingerprint({**cfg, "policy": {**cfg["policy"], "neutral_long_prob": 0.2}}) != fp
    assert config.protocol_fingerprint({**cfg, "classifier": {**cfg["classifier"], "model": "x"}}) != fp
    # Operational settings do not change the procedure.
    assert config.protocol_fingerprint({**cfg, "classifier": {**cfg["classifier"], "rpm": 999}}) == fp


def test_protocol_must_be_numbered_in_order(repo):
    (repo / "protocol.yaml").write_text("sessions:\n  - {n: 1, phase: baseline}\n  - {n: 3, phase: treatment}\n")
    with pytest.raises(ValueError):
        config.load_protocol(config.load_config())


def test_save_accounts_keeps_header(repo):
    cfg = config.load_config()
    accts = config.load_accounts(cfg)
    config.save_accounts(cfg, accts)
    text = (repo / "accounts.yaml").read_text(encoding="utf-8")
    assert text.startswith("# One entry per sock-puppet account.")
    assert config.load_accounts(cfg) == accts
