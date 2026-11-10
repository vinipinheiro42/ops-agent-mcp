from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_agent.cli import main


def test_seed_then_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPS_AGENT_DATA_DIR", str(tmp_path))
    assert main(["seed", "--seed", "42"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["tickets"] == 200 and manifest["docs"] == 30
    assert (tmp_path / "tickets.db").exists() and (tmp_path / "approvers.json").exists()
    assert len(list((tmp_path / "docs").glob("KB-*.md"))) == 30
    assert main(["proposals", "list"]) == 0
    assert main(["audit", "--verify"]) == 0
    assert "audit chain OK" in capsys.readouterr().out


def test_unknown_proposal_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPS_AGENT_DATA_DIR", str(tmp_path))
    main(["seed"])
    capsys.readouterr()
    assert main(["reject", "prop_0000000000000000", "--actor", "pedro.okafor", "--reason", "n/a"]) == 1
    assert "ProposalNotFoundError" in capsys.readouterr().err
