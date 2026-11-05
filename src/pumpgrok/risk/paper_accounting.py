"""Virtual-USDC paper accounting. Pure functions. No ledger and no fills.

The unit is virtual micro-USDC: 1 virtual USDC = 1_000_000 micro units, so
amounts stay integers. This scale is the paper book's own decimal place. It
is not lamports, not a SOL balance, and not an on-chain USDC balance.

No SOL/USDC price is used. A configured 1000 virtual USDC is never treated as
the package ``paper_equity_sol`` default.

Asia/Bangkok is a fixed UTC+7 civil clock. Thailand does not observe
daylight saving, so the offset does not change with the date. Midnight rolls
the day-start equity and the daily entry count. It does not clear a halt.

Nothing here writes a ledger, opens a socket, or builds an order.
``execution_ready`` is false on every result from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal

from pumpgrok.replay.clock import format_ts_utc
from pumpgrok.risk.paper_policy import PaperCostModel, PaperDeskPolicy, current_paper_policy

VIRTUAL_USDC_MICRO = 1_000_000
BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
MARK_MAX_AGE_S = 60.0
SAFETY_IDS: tuple[str, ...] = (
    "mint_authority",
    "freeze_authority",
    "lp_status",
    "transfer_tax",
    "honeypot",
    "top10_concentration",
    "bundler_cluster",
    "deployer_history",
    "token2022_extensions",
    "liquidity_depth",
)

SafetyStatus = Literal["pass", "warn", "fail", "blind"]
SafetyVerdict = Literal["CLEAR", "CONDITIONAL", "KILL"]


@dataclass(frozen=True, slots=True)
class Position:
    """One open paper position at entry cost, in virtual micro-USDC."""

    mint: str
    entry_cost_micro: int


@dataclass(frozen=True, slots=True)
class PositionMark:
    """A caller-supplied conservative mark. This module does not fetch prices.

    ``stress_residual`` means no executable exit inside the 200 bps cap. The
    value is a valuation haircut, not a fill.
    """

    mint: str
    value_micro: int
    kind: Literal["executable_within_cap", "stress_residual"]
    observed_at_epoch: float
    test_data: bool


@dataclass(frozen=True, slots=True)
class SafetyRow:
    """One safety check row. Status ``blind`` is missing evidence."""

    id: str
    status: SafetyStatus
    test_data: bool = False


@dataclass(frozen=True, slots=True)
class PaperBook:
    """In-memory virtual-USDC book. ``initialized`` is a caller's claim.

    This object is not a ledger row. Constructing one does not persist equity
    or a peak.
    """

    unit: str
    cash_micro: int
    positions: tuple[Position, ...]
    peak_micro: int
    day_start_micro: int
    bangkok_day: str
    halt_latched: bool
    halt_reason: str
    entries_today: int
    reconciled: bool
    initialized: bool
    test_data: bool


@dataclass(frozen=True, slots=True)
class EntryIntent:
    """A proposed entry expressed in virtual USDC. Not an order and not a fill."""

    mint: str
    size_virtual_usdc: Decimal
    slippage_bps: int
    stage: str | None
    safety: tuple[SafetyRow, ...]
    test_data: bool


@dataclass(frozen=True, slots=True)
class Readiness:
    """Configuration, data, and execution are separate."""

    configuration_ready: bool
    data_ready: bool
    execution_ready: bool
    data_blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Policy result. ``qualifies`` is not permission to execute."""

    qualifies: bool
    reason: str
    data_blockers: tuple[str, ...]
    rule_failures: tuple[str, ...]
    book: PaperBook
    readiness: Readiness
    all_in_micro: int | None = None


def virtual_usdc_to_micro(amount: Decimal) -> int:
    """Virtual USDC to integer micro units, half up. Rejects negative amounts."""
    if amount < 0:
        raise ValueError("virtual USDC amount must be >= 0")
    scaled = (amount * VIRTUAL_USDC_MICRO).to_integral_value(rounding=ROUND_HALF_UP)
    return int(scaled)


def configured_starting_micro(policy: PaperDeskPolicy) -> int:
    """Starting virtual-USDC budget from the policy, in micro units.

    Missing or non-positive starting equity raises. The SOL package default
    is not consulted.
    """
    amount = policy.starting_equity_virtual_usdc
    if amount is None or amount <= 0:
        raise ValueError("starting virtual USDC equity is missing")
    return virtual_usdc_to_micro(amount)


def bangkok_day(epoch_s: float) -> str:
    """Asia/Bangkok calendar day of an epoch second, ``YYYY-MM-DD``."""
    return datetime.fromtimestamp(epoch_s, BANGKOK).date().isoformat()


def deployment_day(epoch_s: float) -> str:
    """Day key for supervisor rollover.

    Engaged paper deployment uses the Asia/Bangkok civil date. Every other
    posture, including the package and live defaults, keeps the UTC date.
    """
    policy = current_paper_policy()
    if policy.engaged and policy.halt_timezone == "Asia/Bangkok":
        return bangkok_day(epoch_s)
    return format_ts_utc(epoch_s)[:10]


def initialization_prerequisites() -> tuple[str, ...]:
    """What a later authorized initialization must record. This does not do it."""
    return (
        "unit virtual_usdc_micro, separate from SOL lamports",
        "starting cash of 1000 virtual USDC and a positive day-start E0",
        "running peak equal to that equity at the start, then only raised by later verified equity",
        "Asia/Bangkok day of the snapshot",
        "UTC timestamp and source for the snapshot",
        "reconciled external flows, or entries stay blocked",
        "a sourced cost model with source and date before an entry can qualify",
        "no fill, scan, or execution adapter is part of initialization",
    )


def configuration_readiness(policy: PaperDeskPolicy) -> Readiness:
    """Readiness of the loaded policy alone. No book is created."""
    if not policy.engaged:
        return Readiness(False, False, False, ())
    blockers: list[str] = []
    if not policy.cost_model.verified:
        blockers.append("cost_model_unverified")
    blockers.append("equity_not_initialized")
    blockers.append("market_evidence_not_loaded")
    return Readiness(
        configuration_ready=True,
        data_ready=False,
        execution_ready=False,
        data_blockers=tuple(blockers),
    )


def execution_refusal(
    policy: PaperDeskPolicy,
    *,
    slippage_bps: int | None = None,
    exit_reason: str | None = None,
) -> str | None:
    """Execution-boundary refusal. None when the policy is disengaged.

    Blind flatten and slippage above the cap are named. Every other engaged
    order is ``execution_not_authorized`` so this build does not reach a fill.
    """
    if not policy.engaged:
        return None
    if exit_reason == "blind_flatten":
        return "blind_flatten_prohibited"
    if slippage_bps is not None and slippage_bps > policy.max_slippage_bps:
        return "slippage_above_cap"
    return "execution_not_authorized"


def safety_verdict(rows: tuple[SafetyRow, ...]) -> SafetyVerdict:
    """Any missing, blind, or failed required check is KILL.

    A warning is CONDITIONAL and is not a CLEAR. This gate does not use an
    advisory-BLIND exception.
    """
    by_id = {row.id: row.status for row in rows}
    if any(check_id not in by_id for check_id in SAFETY_IDS):
        return "KILL"
    if any(by_id[check_id] in ("blind", "fail") for check_id in SAFETY_IDS):
        return "KILL"
    if any(by_id[check_id] == "warn" for check_id in SAFETY_IDS):
        return "CONDITIONAL"
    return "CLEAR"


def equity_micro(book: PaperBook, marks: tuple[PositionMark, ...]) -> int | None:
    """Cash plus conservative marks. None when any open position is unmarked."""
    by_mint = {mark.mint: mark for mark in marks}
    total = book.cash_micro
    for position in book.positions:
        mark = by_mint.get(position.mint)
        if mark is None:
            return None
        total += mark.value_micro
    return total


def roll_bangkok_day(book: PaperBook, now_epoch: float, equity: int) -> PaperBook:
    """On a new Bangkok day, set E0 and clear the daily entry count.

    A latched halt stays latched. The running peak is left as it is.
    """
    day = bangkok_day(now_epoch)
    if day == book.bangkok_day:
        return book
    return replace(
        book,
        bangkok_day=day,
        day_start_micro=equity,
        entries_today=0,
    )


def clear_halt(book: PaperBook, *, human: bool) -> PaperBook:
    """Clear a latched halt. A non-human caller cannot clear it."""
    if not human:
        raise ValueError("halt clear requires a human")
    return replace(book, halt_latched=False, halt_reason="")


def apply_external_flow(book: PaperBook, amount_micro: int) -> PaperBook:
    """Adjust cash, day-start, and peak by the same external flow.

    A deposit does not create a trading peak and does not erase a loss.
    The caller still has to reconcile the flow before entries qualify.
    """
    return replace(
        book,
        cash_micro=book.cash_micro + amount_micro,
        day_start_micro=book.day_start_micro + amount_micro,
        peak_micro=book.peak_micro + amount_micro,
        reconciled=False,
    )


def mark_reconciled(book: PaperBook) -> PaperBook:
    """Record that an external flow was reconciled. Does not write a ledger."""
    return replace(book, reconciled=True)


def _ratio_at_least(numerator: int, denominator: int, limit: Decimal) -> bool:
    """True when numerator/denominator >= limit. Non-positive denominator fails closed."""
    if denominator <= 0:
        return True
    return Fraction(numerator, denominator) >= Fraction(limit)


def _all_in_micro(size_micro: int, slippage_bps: int, cost: PaperCostModel) -> int:
    fee_rate = cost.venue_fee_rate_per_side or Decimal(0)
    fee = int((Decimal(size_micro) * fee_rate).to_integral_value(rounding=ROUND_HALF_UP))
    slip = int(
        (Decimal(size_micro) * Decimal(slippage_bps) / Decimal(10_000)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    network = virtual_usdc_to_micro(cost.network_cost_virtual_usdc_per_side or Decimal(0))
    return size_micro + fee + slip + network


def _latch_for_equity(book: PaperBook, equity: int, policy: PaperDeskPolicy) -> PaperBook:
    if book.halt_latched or book.day_start_micro <= 0 or book.peak_micro <= 0:
        return book
    loss = book.day_start_micro - equity
    if loss > 0 and _ratio_at_least(loss, book.day_start_micro, policy.daily_loss_halt_fraction):
        return replace(book, halt_latched=True, halt_reason="daily_loss")
    draw = book.peak_micro - equity
    if draw > 0 and _ratio_at_least(draw, book.peak_micro, policy.drawdown_halt_fraction):
        return replace(book, halt_latched=True, halt_reason="drawdown")
    if equity > book.peak_micro:
        return replace(book, peak_micro=equity)
    return book


def _data_gaps(book: PaperBook, policy: PaperDeskPolicy) -> list[str]:
    gaps: list[str] = []
    if book.unit != "virtual_usdc_micro":
        gaps.append("unit_not_virtual_usdc")
    if not book.initialized:
        gaps.append("equity_not_initialized")
    if not policy.cost_model.verified:
        gaps.append("cost_model_unverified")
    return gaps


def evaluate_entry(
    book: PaperBook,
    intent: EntryIntent,
    policy: PaperDeskPolicy,
    *,
    now_epoch: float,
    marks: tuple[PositionMark, ...] = (),
) -> Evaluation:
    """Qualify one entry against the virtual-USDC book.

    The returned book may be day-rolled or halt-latched. Nothing is persisted
    and ``execution_ready`` stays false.
    """
    data = _data_gaps(book, policy)
    rules: list[str] = []
    if not book.reconciled:
        rules.append("external_flow_unreconciled")
    current = equity_micro(book, marks)
    if book.positions and current is None:
        data.append("unmarked_position")
    if current is None:
        data.append("market_evidence_missing")
    working = book
    if current is not None and book.initialized:
        working = roll_bangkok_day(working, now_epoch, current)
        working = _latch_for_equity(working, current, policy)
    if _marks_block(book, marks, now_epoch):
        data.append("stale_mark")
    if any(mark.kind == "stress_residual" for mark in marks):
        rules.append("residual_illiquidity")
    _append_rule_failures(working, intent, policy, rules)
    reason = _reason(data, rules, working)
    qualifies = not data and not rules and not working.halt_latched
    if working.halt_latched and reason == "pass":
        reason = working.halt_reason or "halt_latched"
        qualifies = False
    blockers = tuple(dict.fromkeys(data))
    failures = tuple(dict.fromkeys(rules))
    all_in = None
    if policy.cost_model.verified and "unit_not_virtual_usdc" not in data:
        charged = max(intent.slippage_bps, policy.cost_model.entry_slippage_bps or 0)
        all_in = _all_in_micro(
            virtual_usdc_to_micro(intent.size_virtual_usdc),
            charged,
            policy.cost_model,
        )
    return Evaluation(
        qualifies=qualifies,
        reason=reason,
        data_blockers=blockers,
        rule_failures=failures,
        book=working,
        readiness=Readiness(
            configuration_ready=policy.engaged,
            data_ready=not blockers,
            execution_ready=False,
            data_blockers=blockers,
        ),
        all_in_micro=all_in,
    )


def _marks_block(book: PaperBook, marks: tuple[PositionMark, ...], now_epoch: float) -> bool:
    if not book.positions:
        return False
    by_mint = {mark.mint: mark for mark in marks}
    for position in book.positions:
        mark = by_mint.get(position.mint)
        if mark is None:
            return False
        if now_epoch - mark.observed_at_epoch > MARK_MAX_AGE_S:
            return True
    return False


def _append_rule_failures(
    book: PaperBook,
    intent: EntryIntent,
    policy: PaperDeskPolicy,
    rules: list[str],
) -> None:
    rules.extend(_state_rules(book, policy))
    rules.extend(_intent_rules(book, intent, policy))
    if policy.cost_model.verified:
        rules.extend(_size_rules(book, intent, policy))


def _state_rules(book: PaperBook, policy: PaperDeskPolicy) -> tuple[str, ...]:
    found: list[str] = []
    if book.halt_latched:
        found.append(book.halt_reason or "halt_latched")
    if book.day_start_micro <= 0:
        found.append("e0_not_positive")
    if book.entries_today >= policy.max_trades_per_day:
        found.append("max_trades_per_day")
    if len(book.positions) >= policy.runtime_position_cap:
        found.append("max_open_positions")
    return tuple(found)


def _intent_rules(book: PaperBook, intent: EntryIntent, policy: PaperDeskPolicy) -> tuple[str, ...]:
    found: list[str] = []
    if intent.slippage_bps > policy.max_slippage_bps:
        found.append("slippage_above_cap")
    if intent.stage == "bonding_curve":
        found.append("pre_migration_excluded")
    elif intent.stage != "graduated":
        found.append("migration_evidence_missing")
    verdict = safety_verdict(intent.safety)
    if verdict == "KILL":
        found.append("safety_kill")
    elif verdict == "CONDITIONAL":
        found.append("safety_not_clear")
    if any(position.mint == intent.mint for position in book.positions):
        found.append("already_open")
    return tuple(found)


def _size_rules(book: PaperBook, intent: EntryIntent, policy: PaperDeskPolicy) -> tuple[str, ...]:
    found: list[str] = []
    charged = max(intent.slippage_bps, policy.cost_model.entry_slippage_bps or 0)
    trade_cap = virtual_usdc_to_micro(policy.max_trade_virtual_usdc or Decimal(0))
    all_in = _all_in_micro(
        virtual_usdc_to_micro(intent.size_virtual_usdc),
        min(charged, policy.max_slippage_bps),
        policy.cost_model,
    )
    if all_in > trade_cap:
        found.append("size_above_cap")
    spent = sum(position.entry_cost_micro for position in book.positions)
    if (
        spent + virtual_usdc_to_micro(intent.size_virtual_usdc)
        > policy.runtime_position_cap * trade_cap
    ):
        found.append("gross_exposure_cap")
    return tuple(found)


def _reason(data: list[str], rules: list[str], book: PaperBook) -> str:
    if data:
        return data[0]
    if rules:
        return rules[0]
    if book.halt_latched:
        return book.halt_reason or "halt_latched"
    return "pass"


def loss_fraction(day_start_micro: int, equity_micro_now: int) -> Decimal:
    """max(0, (E0 - Et) / E0). Non-positive E0 raises."""
    if day_start_micro <= 0:
        raise ValueError("day-start equity must be positive")
    loss = day_start_micro - equity_micro_now
    if loss <= 0:
        return Decimal(0)
    return Decimal(loss) / Decimal(day_start_micro)


def drawdown_fraction(peak_micro: int, equity_micro_now: int) -> Decimal:
    """(peak - equity) / peak, floored at 0. Non-positive peak raises."""
    if peak_micro <= 0:
        raise ValueError("peak equity must be positive")
    gap = peak_micro - equity_micro_now
    if gap <= 0:
        return Decimal(0)
    return Decimal(gap) / Decimal(peak_micro)


def utc_iso(epoch_s: float) -> str:
    """UTC timestamp for a test fixture or a caller-supplied epoch. No clock read."""
    return datetime.fromtimestamp(epoch_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
