"""Virtual-USDC policy accounting on synthetic fixtures.

Every book, mark, cost, and safety row in this file is TEST DATA. Nothing
here writes a ledger, builds an order, or executes a fill.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pumpgrok.core.units import sol_to_lamports
from pumpgrok.risk.paper_accounting import (
    SAFETY_IDS,
    VIRTUAL_USDC_MICRO,
    EntryIntent,
    PaperBook,
    Position,
    PositionMark,
    SafetyRow,
    apply_external_flow,
    bangkok_day,
    clear_halt,
    configuration_readiness,
    configured_starting_micro,
    drawdown_fraction,
    evaluate_entry,
    initialization_prerequisites,
    loss_fraction,
    mark_reconciled,
    roll_bangkok_day,
    safety_verdict,
)
from pumpgrok.risk.paper_policy import PaperCostModel, PaperDeskPolicy

TEST_SOURCE = "TEST DATA synthetic fixture"
MICRO = 1000 * VIRTUAL_USDC_MICRO
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC).timestamp()
BANGKOK_MIDNIGHT = datetime(2026, 11, 5, 17, 0, tzinfo=UTC).timestamp()
BEFORE_MIDNIGHT = datetime(2026, 11, 5, 16, 59, 59, tzinfo=UTC).timestamp()


def _policy(*, verified: bool = True, fee: str = "0") -> PaperDeskPolicy:
    cost = PaperCostModel(
        verified=verified,
        source=TEST_SOURCE,
        as_of="2026-11-05",
        venue_fee_rate_per_side=Decimal(fee),
        network_cost_virtual_usdc_per_side=Decimal("0"),
        entry_slippage_bps=0,
        stress_exit_haircut_bps=500,
    )
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
        cost_model=cost,
    )


def _book(**overrides: object) -> PaperBook:
    book = PaperBook(
        unit="virtual_usdc_micro",
        cash_micro=MICRO,
        positions=(),
        peak_micro=MICRO,
        day_start_micro=MICRO,
        bangkok_day="2026-11-05",
        halt_latched=False,
        halt_reason="",
        entries_today=0,
        reconciled=True,
        initialized=True,
        test_data=True,
    )
    return replace(book, **overrides)  # type: ignore[arg-type]


def _safety(status: str = "pass", drop: str | None = None) -> tuple[SafetyRow, ...]:
    rows = [
        SafetyRow(id=name, status="pass", test_data=True)  # type: ignore[arg-type]
        for name in SAFETY_IDS
        if name != drop
    ]
    if status != "pass" and rows:
        rows[0] = SafetyRow(id=rows[0].id, status=status, test_data=True)  # type: ignore[arg-type]
    return tuple(rows)


def _intent(**overrides: object) -> EntryIntent:
    intent = EntryIntent(
        mint="TEST-MINT",
        size_virtual_usdc=Decimal("1"),
        slippage_bps=50,
        stage="graduated",
        safety=_safety(),
        test_data=True,
    )
    return replace(intent, **overrides)  # type: ignore[arg-type]


def test_starting_virtual_usdc_is_not_the_package_sol_default() -> None:
    micro = configured_starting_micro(_policy())
    assert micro == MICRO
    assert micro != sol_to_lamports(Decimal("10"))
    notes = initialization_prerequisites()
    assert any("virtual_usdc_micro" in note for note in notes)
    ready = configuration_readiness(_policy(verified=False))
    assert ready.configuration_ready is True
    assert ready.data_ready is False
    assert ready.execution_ready is False
    assert "cost_model_unverified" in ready.data_blockers
    assert "equity_not_initialized" in ready.data_blockers


def test_unverified_cost_blocks_qualification_until_a_complete_model_is_supplied() -> None:
    blocked = evaluate_entry(_book(), _intent(), _policy(verified=False), now_epoch=NOW)
    assert blocked.qualifies is False
    assert blocked.reason == "cost_model_unverified"
    assert blocked.readiness.execution_ready is False
    opened = evaluate_entry(_book(), _intent(), _policy(verified=True), now_epoch=NOW)
    assert opened.qualifies is True
    assert opened.reason == "pass"
    assert opened.readiness.data_ready is True
    assert opened.readiness.execution_ready is False
    assert opened.book.entries_today == 0
    assert opened.book.test_data is True


def test_bangkok_midnight_rolls_e0_and_keeps_the_latch() -> None:
    assert bangkok_day(BEFORE_MIDNIGHT) == "2026-11-05"
    assert bangkok_day(BANGKOK_MIDNIGHT) == "2026-11-06"
    book = _book(halt_latched=True, halt_reason="daily_loss", entries_today=4)
    rolled = roll_bangkok_day(book, BANGKOK_MIDNIGHT, MICRO)
    assert rolled.bangkok_day == "2026-11-06"
    assert rolled.day_start_micro == MICRO
    assert rolled.entries_today == 0
    assert rolled.halt_latched is True
    assert rolled.halt_reason == "daily_loss"
    assert rolled.peak_micro == book.peak_micro
    with pytest.raises(ValueError, match="human"):
        clear_halt(rolled, human=False)
    cleared = clear_halt(rolled, human=True)
    assert cleared.halt_latched is False


def test_two_percent_loss_and_ten_percent_drawdown_latch() -> None:
    assert loss_fraction(MICRO, 980 * VIRTUAL_USDC_MICRO) == Decimal("0.02")
    assert loss_fraction(MICRO, (980 * VIRTUAL_USDC_MICRO) + 1) < Decimal("0.02")
    at_limit = evaluate_entry(
        _book(cash_micro=980 * VIRTUAL_USDC_MICRO),
        _intent(),
        _policy(),
        now_epoch=NOW,
    )
    assert at_limit.book.halt_latched is True
    assert at_limit.book.halt_reason == "daily_loss"
    assert at_limit.qualifies is False
    under = evaluate_entry(
        _book(cash_micro=(980 * VIRTUAL_USDC_MICRO) + 1),
        _intent(),
        _policy(),
        now_epoch=NOW,
    )
    assert under.book.halt_latched is False
    assert drawdown_fraction(MICRO, 900 * VIRTUAL_USDC_MICRO) == Decimal("0.10")
    deep = evaluate_entry(
        _book(
            cash_micro=900 * VIRTUAL_USDC_MICRO,
            day_start_micro=900 * VIRTUAL_USDC_MICRO,
            peak_micro=MICRO,
        ),
        _intent(),
        _policy(),
        now_epoch=NOW,
    )
    assert deep.book.halt_reason == "drawdown"
    assert deep.qualifies is False


def test_deposit_keeps_the_loss_amount_and_the_latch() -> None:
    underwater = _book(
        cash_micro=980 * VIRTUAL_USDC_MICRO,
        halt_latched=True,
        halt_reason="daily_loss",
    )
    flowed = apply_external_flow(underwater, 20 * VIRTUAL_USDC_MICRO)
    assert flowed.day_start_micro - flowed.cash_micro == 20 * VIRTUAL_USDC_MICRO
    assert flowed.halt_latched is True
    assert flowed.reconciled is False
    blocked = evaluate_entry(flowed, _intent(), _policy(), now_epoch=NOW)
    assert blocked.qualifies is False
    assert "external_flow_unreconciled" in blocked.rule_failures
    released = evaluate_entry(mark_reconciled(flowed), _intent(), _policy(), now_epoch=NOW)
    assert released.book.halt_latched is True
    assert released.qualifies is False


def test_safety_blind_and_missing_evidence_are_kill() -> None:
    blind = _safety()
    blind_rows = list(blind)
    blind_rows[5] = SafetyRow(id="top10_concentration", status="blind", test_data=True)
    assert safety_verdict(tuple(blind_rows)) == "KILL"
    assert safety_verdict(_safety(drop="deployer_history")) == "KILL"
    assert safety_verdict(_safety()) == "CLEAR"
    blocked = evaluate_entry(
        _book(),
        _intent(safety=tuple(blind_rows)),
        _policy(),
        now_epoch=NOW,
    )
    assert blocked.qualifies is False
    assert blocked.reason == "safety_kill"


def test_size_cap_positions_and_pre_migration() -> None:
    full = evaluate_entry(
        _book(),
        _intent(size_virtual_usdc=Decimal("10")),
        _policy(fee="0.01"),
        now_epoch=NOW,
    )
    assert full.qualifies is False
    assert full.reason == "size_above_cap"
    assert (full.all_in_micro or 0) > 10 * VIRTUAL_USDC_MICRO
    three = tuple(
        Position(mint=f"TEST-{index}", entry_cost_micro=VIRTUAL_USDC_MICRO) for index in range(3)
    )
    unmarked = evaluate_entry(_book(positions=three), _intent(), _policy(), now_epoch=NOW)
    assert unmarked.reason == "unmarked_position"
    marked = tuple(
        PositionMark(
            mint=position.mint,
            value_micro=position.entry_cost_micro,
            kind="executable_within_cap",
            observed_at_epoch=NOW,
            test_data=True,
        )
        for position in three
    )
    capped = evaluate_entry(
        _book(positions=three), _intent(), _policy(), now_epoch=NOW, marks=marked
    )
    assert capped.qualifies is False
    assert "max_open_positions" in capped.rule_failures
    duplicate = evaluate_entry(
        _book(positions=(Position(mint="TEST-MINT", entry_cost_micro=VIRTUAL_USDC_MICRO),)),
        _intent(),
        _policy(),
        now_epoch=NOW,
        marks=(
            PositionMark(
                mint="TEST-MINT",
                value_micro=VIRTUAL_USDC_MICRO,
                kind="executable_within_cap",
                observed_at_epoch=NOW,
                test_data=True,
            ),
        ),
    )
    assert duplicate.reason == "already_open"
    curve = evaluate_entry(_book(), _intent(stage="bonding_curve"), _policy(), now_epoch=NOW)
    assert curve.reason == "pre_migration_excluded"
    wide = evaluate_entry(_book(), _intent(slippage_bps=201), _policy(), now_epoch=NOW)
    assert wide.reason == "slippage_above_cap"
    assert _policy().cost_model.stress_exit_haircut_bps == 500
    assert _policy().max_slippage_bps == 200
