"""Wiring tests for the engaged paper policy.

The supervisor rollover is invoked on an in-memory store. The safety scorer
is invoked with patched readers so no RPC client sends a request. No fill
row is written.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from pumpgrok.core.risk_budget import RiskBudget
from pumpgrok.risk.paper_accounting import deployment_day
from pumpgrok.risk.paper_policy import PaperDeskPolicy, bind_paper_policy
from pumpgrok.supervisor.scheduler.supervisor import Supervisor, rollover_key
from pumpgrok.tools.safety import score_report
from pumpgrok.tools.safety.checks import CHECK_IDS, CheckResult
from pumpgrok.tools.safety.score_report import (
    ScoreReportIn,
    _apply_engaged_safety,
    _Readings,
    run_score_report,
)

MINT = "So11111111111111111111111111111111111111112"
BOUNDARY = datetime(2026, 11, 5, 17, 0, tzinfo=UTC).timestamp()


@pytest.fixture(autouse=True)
def _reset_policy() -> Iterator[None]:
    yield
    bind_paper_policy(PaperDeskPolicy())


def _engaged() -> PaperDeskPolicy:
    return PaperDeskPolicy(
        engaged=True,
        denomination="virtual_usdc",
        unit="virtual_usdc_micro",
        starting_equity_virtual_usdc=Decimal("1000"),
        max_trade_virtual_usdc=Decimal("10"),
        user_position_ceiling=5,
        runtime_position_cap=3,
        halt_timezone="Asia/Bangkok",
        max_slippage_bps=200,
        daily_loss_halt_fraction=Decimal("0.02"),
        drawdown_halt_fraction=Decimal("0.10"),
        max_trades_per_day=10,
        blind_is_kill=True,
    )


class _MemoryStore:
    """Records appends. It is not a ledger file."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def read(self, since_seq: int = 1, limit: int | None = None) -> list[dict[str, Any]]:
        del since_seq, limit
        return []

    def get_meta(self, key: str) -> None:
        del key
        return None

    def append(self, **row: Any) -> None:
        self.rows.append(row)


class _Host:
    def __init__(self, store: _MemoryStore) -> None:
        self._store = store
        self._budget = RiskBudget()
        self._source = "paper"
        self._correlation_id = "wiring-test"
        self._last_rollover_day: str | None = None
        self._day_start_equity = lambda now: Supervisor._day_start_equity(self, now)  # type: ignore[arg-type]


def _row(check_id: str, status: str) -> CheckResult:
    return CheckResult(
        id=check_id,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        weight=Decimal("0"),
        reason="TEST DATA wiring row",
    )


def _holder_blind_rows() -> list[CheckResult]:
    """Pass every check except the holder pair, which is BLIND."""
    rows: list[CheckResult] = []
    for check_id in CHECK_IDS:
        status = "blind" if check_id in {"top10_concentration", "deployer_history"} else "pass"
        rows.append(_row(check_id, status))
    return rows


def test_rollover_uses_bangkok_only_while_the_paper_policy_is_engaged() -> None:
    assert deployment_day(BOUNDARY) == "2026-11-05"
    store = _MemoryStore()
    host = _Host(store)
    Supervisor._emit_rollover(host, BOUNDARY)  # type: ignore[arg-type]
    assert len(store.rows) == 1
    assert store.rows[0]["type"] == "DailyRollover"
    assert store.rows[0]["payload"]["day_utc"] == "2026-11-05"
    assert store.rows[0]["idempotency_key"] == rollover_key("2026-11-05")

    bind_paper_policy(_engaged())
    assert deployment_day(BOUNDARY) == "2026-11-06"
    engaged_store = _MemoryStore()
    engaged_host = _Host(engaged_store)
    Supervisor._emit_rollover(engaged_host, BOUNDARY)  # type: ignore[arg-type]
    assert engaged_store.rows[0]["type"] == "DailyRollover"
    assert engaged_store.rows[0]["payload"]["day_utc"] == "2026-11-06"
    assert engaged_store.rows[0]["idempotency_key"] == rollover_key("2026-11-06")
    Supervisor._emit_rollover(engaged_host, BOUNDARY)  # type: ignore[arg-type]
    assert len(engaged_store.rows) == 1
    assert all(row["type"] != "FillRecorded" for row in store.rows + engaged_store.rows)


def test_apply_engaged_safety_forces_kill_and_clears_the_advisory_list() -> None:
    rows = _holder_blind_rows()
    verdict, advisory = _apply_engaged_safety(
        rows, verdict="CLEAR", advisory_blind_checks=["top10_concentration", "deployer_history"]
    )
    assert verdict == "CLEAR"
    assert advisory == ["top10_concentration", "deployer_history"]
    bind_paper_policy(_engaged())
    strict_verdict, strict_advisory = _apply_engaged_safety(
        rows, verdict="CLEAR", advisory_blind_checks=["top10_concentration", "deployer_history"]
    )
    assert strict_verdict == "KILL"
    assert strict_advisory == []


@pytest.mark.asyncio
async def test_score_report_calls_the_strict_gate_without_a_network_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"apply": 0, "read": 0}
    real_apply = score_report._apply_engaged_safety

    def _wrapped(rows, **kwargs):  # type: ignore[no-untyped-def]
        calls["apply"] += 1
        return real_apply(rows, **kwargs)

    async def _no_network(*args: object, **kwargs: object) -> _Readings:
        calls["read"] += 1
        del args, kwargs
        return _Readings(None, None, None, None, None, None)

    def _rows(*args: object, **kwargs: object) -> list[CheckResult]:
        del args, kwargs
        return _holder_blind_rows()

    def _no_extra(*args: object, **kwargs: object) -> list[CheckResult]:
        del args, kwargs
        return []

    monkeypatch.setattr(score_report, "_apply_engaged_safety", _wrapped)
    monkeypatch.setattr(score_report, "_read_all", _no_network)
    monkeypatch.setattr(score_report, "_reader_rows", _rows)
    monkeypatch.setattr(score_report, "_advisory_rows", _no_extra)
    monkeypatch.setattr(score_report, "paid_rpc_provider_recorded", lambda: False)

    class _Rpc:
        def now(self) -> float:
            return BOUNDARY

        @property
        def clock(self) -> _Rpc:
            return self

    payload = ScoreReportIn(mint=MINT, curve_account=MINT, intended_size_lamports=0)
    disengaged = await run_score_report(payload, _Rpc())  # type: ignore[arg-type]
    assert disengaged["verdict_recommendation"] == "CLEAR"
    assert "top10_concentration" in disengaged["advisory_blind_checks"]

    bind_paper_policy(_engaged())
    engaged = await run_score_report(payload, _Rpc())  # type: ignore[arg-type]
    assert engaged["verdict_recommendation"] == "KILL"
    assert engaged["advisory_blind_checks"] == []
    assert calls["apply"] == 2
    assert calls["read"] == 2
    assert "FillRecorded" not in engaged
