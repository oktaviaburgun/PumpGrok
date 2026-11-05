"""Operator paper overlay: load, refuse, and stay fail-closed.

Offline only. These tests set PUMPGROK_CONFIG, load settings, and refuse CLI
verbs. They do not scan, fill, open a socket, or start a desk cycle.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from pumpgrok.cli import main
from pumpgrok.cli.backtest import _backtest_verb
from pumpgrok.cli.verbs_ops import _live_verb, _verify_verb
from pumpgrok.cli.verbs_state import _killswitch, _paper, _run, _state, _ticket_verb
from pumpgrok.config.settings import ConfigError, Settings, load_settings, overlay_path
from pumpgrok.core.errors import CorruptState
from pumpgrok.doctor import doctor_report
from pumpgrok.ledger.store import LedgerStore
from pumpgrok.risk.paper_policy import (
    STANDING_BLOCKERS,
    PaperDeskPolicy,
    bind_paper_policy,
    entry_blockers,
)
from pumpgrok.tools import build_operator_registry, ensure_paper_genesis

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "pumpgrok"
    / "config"
    / "examples"
    / "paper-operator.example.toml"
)


@pytest.fixture(autouse=True)
def _reset_policy() -> Iterator[None]:
    yield
    bind_paper_policy(PaperDeskPolicy())


def _load_example(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("PUMPGROK_MODE", raising=False)
    monkeypatch.delenv("PUMPGROK_PROFILE", raising=False)
    monkeypatch.delenv("PUMPGROK_HEALTH__PORT", raising=False)
    monkeypatch.setenv("PUMPGROK_CONFIG", str(EXAMPLE))
    return load_settings()


def test_package_paper_overlay_is_unchanged() -> None:
    """The in-package paper profile keeps its previous risk defaults."""
    text = overlay_path("paper").read_text(encoding="utf-8")
    assert 'daily_loss_halt_fraction = "0.05"' in text
    assert "max_open_positions = 3" in text
    assert "stop_loss_pct = 30.0" in text
    assert "take_profit_pct = 120.0" in text


def test_disengaged_defaults_do_not_apply_the_operator_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUMPGROK_CONFIG", raising=False)
    monkeypatch.delenv("PUMPGROK_MODE", raising=False)
    settings = load_settings()
    assert settings.paper_policy.engaged is False
    assert settings.effective_mode == "paper"
    assert settings.risk.daily_loss_halt_fraction == Decimal("0.05")
    assert settings.risk.drawdown_halt_fraction == Decimal("0.15")
    assert settings.risk.max_open_positions == 3
    assert settings.exits.stop_loss_pct == 30.0
    assert settings.limits.max_slippage_bps == 300
    assert entry_blockers(settings.paper_policy) == ()
    assert doctor_report(settings)[1] == "info  paper_policy=disengaged"


def test_example_file_has_no_endpoint_or_exit_proposal() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert text.isascii()
    lowered = text.lower()
    for banned in ("http://", "https://", "wss://", "stop_loss", "take_profit", "private"):
        assert banned not in lowered
    assert "max_open_positions = 3" in text
    assert "user_position_ceiling = 5" in text
    assert 'starting_equity_virtual_usdc = "1000"' in text
    assert 'max_trade_virtual_usdc = "10"' in text


def test_engaged_overlay_loads_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load_example(monkeypatch)
    assert settings.paper_policy.engaged is True
    assert settings.effective_mode == "paper"
    assert settings.live.enabled is False
    assert settings.wallet.allowlist == ()
    assert settings.rpc.endpoints == []
    assert settings.risk.max_trade_fraction == Decimal("0.01")
    assert settings.risk.max_token_fraction == Decimal("0.01")
    assert settings.risk.daily_loss_halt_fraction == Decimal("0.02")
    assert settings.risk.drawdown_halt_fraction == Decimal("0.10")
    assert settings.risk.max_open_positions == 3
    assert settings.risk.max_trades_per_day == 10
    assert settings.risk.liquidity_floor_multiple == 20
    assert settings.risk.partial_fill_fraction == Decimal("0.02")
    assert settings.risk.paper_equity_sol == Decimal("10")
    assert settings.paper_policy.starting_equity_virtual_usdc == Decimal("1000")
    assert settings.paper_policy.max_trade_virtual_usdc == Decimal("10")
    assert settings.paper_policy.user_position_ceiling == 5
    assert settings.paper_policy.runtime_position_cap == 3
    assert settings.paper_policy.halt_timezone == "Asia/Bangkok"
    assert settings.limits.max_slippage_bps == 200
    assert settings.exits.stop_loss_pct == 30.0
    assert settings.exits.take_profit_pct == 120.0
    assert settings.exits.trailing_stop_pct == 35.0
    assert settings.exits.max_hold_seconds == 3600.0
    assert settings.exits.stop_loss_poll_seconds == 15.0
    assert settings.safety.thresholds.transfer_tax_hard_fail_pct == 0.0
    assert settings.safety.thresholds.top10_concentration_fail_pct == 20.0
    assert settings.safety.thresholds.creator_share_veto == 0.10
    assert settings.safety.thresholds.liquidity_multiple_min == 20.0
    assert entry_blockers(settings.paper_policy) == STANDING_BLOCKERS
    report = "\n".join(doctor_report(settings))
    assert "NOT OPERATIONAL" in report
    assert "entries=blocked" in report
    assert "currency_unresolved" in report
    assert "cost_model_unverified" in report
    assert "equity_peak_unverified" in report
    assert report.splitlines()[-1].startswith("doctor:")


def test_engaged_overlay_refuses_desk_verbs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _load_example(monkeypatch)
    empty = argparse.Namespace()
    for action in (_run, _paper, _state, _killswitch, _ticket_verb, _live_verb, _verify_verb):
        with pytest.raises(ConfigError, match="entries stay blocked"):
            action(empty, settings)
    with pytest.raises(ConfigError, match="entries stay blocked"):
        _backtest_verb(empty, settings)
    assert main(["run"]) == 1
    assert main(["doctor"]) == 0
    captured = capsys.readouterr()
    assert "NOT OPERATIONAL" in captured.out
    assert "entries stay blocked" in captured.err
    assert not (tmp_path / "var").exists()


def test_engaged_registry_and_genesis_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _load_example(monkeypatch)
    data_dir = tmp_path / "data"
    with pytest.raises(CorruptState) as caught:
        build_operator_registry(
            data_dir=data_dir,
            repo_root=tmp_path,
            settings=settings,
        )
    assert caught.value.fields.get("paper_policy") == "registry_refused"
    assert not data_dir.exists()

    store = LedgerStore(str(tmp_path / "ledger.sqlite"))
    try:
        with pytest.raises(CorruptState) as genesis:
            ensure_paper_genesis(store, settings.risk)
        assert genesis.value.fields.get("paper_policy") == "equity_unverified"
        assert store.get_meta("initial_equity_lamports") is None
        assert store.count() == 0
    finally:
        store.close()


def _reject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> ConfigError:
    path = tmp_path / "overlay.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.delenv("PUMPGROK_MODE", raising=False)
    monkeypatch.setenv("PUMPGROK_CONFIG", str(path))
    with pytest.raises(ConfigError) as caught:
        load_settings()
    return caught.value


def test_looser_positions_daily_loss_equity_and_exits_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = EXAMPLE.read_text(encoding="utf-8")
    positions = _reject(
        monkeypatch, tmp_path, base.replace("max_open_positions = 3", "max_open_positions = 5", 1)
    )
    assert any("max_open_positions" in problem for problem in positions.problems)
    daily = _reject(
        monkeypatch,
        tmp_path,
        base.replace('daily_loss_halt_fraction = "0.02"', 'daily_loss_halt_fraction = "0.05"'),
    )
    assert any("daily_loss_halt_fraction" in problem for problem in daily.problems)
    equity = _reject(
        monkeypatch,
        tmp_path,
        base.replace("[risk]\n", '[risk]\npaper_equity_sol = "1"\n', 1),
    )
    assert any("paper_equity_sol" in problem for problem in equity.problems)
    exits = _reject(monkeypatch, tmp_path, base + "\n[exits]\nstop_loss_pct = 20.0\n")
    assert any("exit proposals" in problem for problem in exits.problems)


def test_cleared_blockers_and_live_mode_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = EXAMPLE.read_text(encoding="utf-8")
    cleared = _reject(
        monkeypatch,
        tmp_path,
        base.replace("cost_model_verified = false", "cost_model_verified = true"),
    )
    assert any("cost_model_verified" in problem for problem in cleared.problems)
    monkeypatch.setenv("PUMPGROK_CONFIG", str(EXAMPLE))
    monkeypatch.setenv("PUMPGROK_MODE", "live")
    with pytest.raises(ConfigError) as live_mode:
        load_settings()
    assert any("effective mode must be paper" in problem for problem in live_mode.value.problems)


def test_missing_rpc_clear_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base = EXAMPLE.read_text(encoding="utf-8")
    stripped = base.replace("[rpc]\nendpoints = []\n\n", "")
    rejected = _reject(monkeypatch, tmp_path, stripped)
    assert any("rpc.endpoints" in problem for problem in rejected.problems)
