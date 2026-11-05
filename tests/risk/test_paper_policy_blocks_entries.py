"""Engaged paper policy refuses a buy without a fill or a ledger row.

Offline only. The order object is built in memory and refused before the
19-step walk. No adapter, no market read, no desk cycle.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from pumpgrok.core.adapter import OrderRequest, Side
from pumpgrok.core.ids import IdFactory
from pumpgrok.core.risk_budget import RiskBudget
from pumpgrok.ledger.store import LedgerStore
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.risk.engine import STEP_IDS, RiskEngine
from pumpgrok.risk.paper_policy import (
    STANDING_BLOCKERS,
    PaperDeskPolicy,
    bind_paper_policy,
    entry_blockers,
)

MINT = "So11111111111111111111111111111111111111112"


@pytest.fixture(autouse=True)
def _reset_policy() -> Iterator[None]:
    yield
    bind_paper_policy(PaperDeskPolicy())


def _engaged() -> PaperDeskPolicy:
    return PaperDeskPolicy(
        engaged=True,
        denomination="virtual_usdc",
        starting_equity_virtual_usdc=Decimal("1000"),
        max_trade_virtual_usdc=Decimal("10"),
        user_position_ceiling=5,
        runtime_position_cap=3,
        halt_timezone="Asia/Bangkok",
        max_slippage_bps=200,
    )


def _buy(ids: IdFactory, *, slippage: int) -> OrderRequest:
    return OrderRequest(
        ticket_id="SOL-20260201-001",
        mint=MINT,
        side=Side.BUY,
        decision_id=ids.ulid(),
        size_lamports=1,
        max_slippage_bps=slippage,
        approval_id=f"apr-{ids.ulid()}",
        correlation_id="paper-policy-test",
    )


def test_standing_blockers_cover_the_open_policy_gaps() -> None:
    policy = _engaged()
    assert entry_blockers(policy) == STANDING_BLOCKERS
    assert "slippage_above_cap" not in entry_blockers(policy, slippage_bps=200)
    assert entry_blockers(policy, slippage_bps=201)[-1] == "slippage_above_cap"
    assert entry_blockers(policy, side="sell", slippage_bps=500) == ()
    assert entry_blockers(PaperDeskPolicy(), slippage_bps=500) == ()


def test_check_refuses_the_buy_without_a_ledger_row(tmp_path: Path) -> None:
    policy = _engaged()
    bind_paper_policy(policy)
    clock = FixtureClock(start=1_700_000_000)
    ids = IdFactory(clock, random.Random(1))
    store = LedgerStore(str(tmp_path / "ledger.sqlite"))
    try:
        engine = RiskEngine(
            store=store,
            budget=RiskBudget(),
            ids=ids,
            clock=clock,
            env={},
        )
        before = store.count()
        decision = engine.check(_buy(ids, slippage=200), seat="bot-04-sniper")
        assert decision.passed is False
        assert decision.reason == "paper_policy_blocked"
        assert [row["id"] for row in decision.evaluated] == list(STEP_IDS)
        assert [row["status"] for row in decision.evaluated] == ["skip"] * len(STEP_IDS)
        assert store.count() == before == 0
        kinds = [row["type"] for row in store.read(limit=None)]
        assert kinds == []
    finally:
        store.close()
