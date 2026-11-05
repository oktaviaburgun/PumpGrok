"""Operator paper-policy gate. Fail closed. Not a trade approval.

The engaged policy records the human-fixed virtual USDC inputs and the stricter
limits that already fit the engine. It does not convert USDC to SOL, does not
verify costs or equity, and does not apply the unaccepted exit proposals.

While the policy is engaged, new buys are blocked. The block stays until a
later change can show a sourced valuation, a sourced cost model, and a verified
ledger equity and peak. This module does not write that evidence.
"""

from __future__ import annotations

import contextvars
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pumpgrok.config.sections.core import (
    GrokSettings,
    LiveSettings,
    RpcSettings,
    WalletSettings,
)
from pumpgrok.config.sections.engine import (
    ExitsSettings,
    FilterSettings,
    SafetySettings,
    SafetyThresholds,
)
from pumpgrok.config.sections.ops import HealthSettings, NotifySettings
from pumpgrok.core.risk_budget import RiskBudget
from pumpgrok.risk.limits import EngineLimits

# Standing reasons a buy cannot pass while the operator policy is engaged.
# Order is stable for doctor lines and tests.
STANDING_BLOCKERS: tuple[str, ...] = (
    "currency_unresolved",
    "cost_model_unverified",
    "equity_peak_unverified",
    "halt_day_unaligned",
    "advisory_blind_scorer_unchanged",
    "blind_flatten_not_authorized",
    "pre_migration_excluded",
    "exit_proposals_not_applied",
)


class PaperDeskPolicy(BaseModel):
    """Operator overlay block ``[paper_policy]``.

    ``engaged`` is false unless an operator file turns it on. The false-only
    flags cannot be set true in this schema: a TOML comment is not evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engaged: bool = False
    denomination: str = "unset"
    starting_equity_virtual_usdc: Decimal | None = None
    max_trade_virtual_usdc: Decimal | None = None
    user_position_ceiling: int = Field(default=5, ge=1, le=5)
    runtime_position_cap: int = Field(default=3, ge=0, le=3)
    halt_timezone: str = "UTC"
    max_slippage_bps: int = Field(default=200, ge=1, le=10_000)
    cost_model_verified: bool = False
    equity_peak_verified: bool = False
    sol_usdc_valuation_present: bool = False
    entries_authorized: bool = False
    exits_applied: bool = False
    blind_flatten_authorized: bool = False
    blind_advisory_exception: bool = False

    @field_validator(
        "cost_model_verified",
        "equity_peak_verified",
        "sol_usdc_valuation_present",
        "entries_authorized",
        "exits_applied",
        "blind_flatten_authorized",
        "blind_advisory_exception",
    )
    @classmethod
    def _cannot_clear(cls, value: bool) -> bool:
        if value:
            raise ValueError("cannot be set true in this schema")
        return value

    @model_validator(mode="after")
    def _fixed_inputs_when_engaged(self) -> PaperDeskPolicy:
        if not self.engaged:
            return self
        problems: list[str] = []
        if self.denomination != "virtual_usdc":
            problems.append("denomination")
        if self.starting_equity_virtual_usdc != Decimal("1000"):
            problems.append("starting_equity_virtual_usdc")
        if self.max_trade_virtual_usdc != Decimal("10"):
            problems.append("max_trade_virtual_usdc")
        if self.user_position_ceiling != 5:
            problems.append("user_position_ceiling")
        if self.runtime_position_cap != 3:
            problems.append("runtime_position_cap")
        if self.halt_timezone != "Asia/Bangkok":
            problems.append("halt_timezone")
        if self.max_slippage_bps != 200:
            problems.append("max_slippage_bps")
        if problems:
            raise ValueError(
                "paper policy engaged but fixed inputs mismatch: " + ", ".join(problems)
            )
        return self


_POLICY: contextvars.ContextVar[PaperDeskPolicy | None] = contextvars.ContextVar(
    "pumpgrok_paper_policy",
    default=None,
)


def current_paper_policy() -> PaperDeskPolicy:
    """Policy bound by ``load_settings``, or a disengaged default."""
    bound = _POLICY.get()
    if bound is None:
        return PaperDeskPolicy()
    return bound


def bind_paper_policy(policy: PaperDeskPolicy) -> contextvars.Token[PaperDeskPolicy | None]:
    """Bind the policy for later ``RiskEngine`` construction in this process."""
    return _POLICY.set(policy)


def reset_paper_policy(token: contextvars.Token[PaperDeskPolicy | None]) -> None:
    """Restore the binding captured by ``bind_paper_policy``."""
    _POLICY.reset(token)


class _OverlayView(Protocol):
    """The settings fields the engaged-policy check reads. Not a second schema."""

    risk: RiskBudget
    limits: EngineLimits
    exits: ExitsSettings
    filter: FilterSettings
    safety: SafetySettings
    live: LiveSettings
    wallet: WalletSettings
    rpc: RpcSettings
    notify: NotifySettings
    health: HealthSettings
    grok: GrokSettings

    @property
    def effective_mode(self) -> str: ...


def entry_blockers(
    policy: PaperDeskPolicy,
    *,
    side: str = "buy",
    slippage_bps: int | None = None,
) -> tuple[str, ...]:
    """Buy blockers for an engaged policy. Sells and a disengaged policy add none.

    A slippage figure above the 200 bps cap is an extra blocker. It does not
    remove the standing ones, and it does not shrink the order to fit.
    """
    if not policy.engaged or side != "buy":
        return ()
    blockers = list(STANDING_BLOCKERS)
    if slippage_bps is not None and slippage_bps > policy.max_slippage_bps:
        blockers.append("slippage_above_cap")
    return tuple(blockers)


def paper_policy_report_lines(policy: PaperDeskPolicy) -> list[str]:
    """Doctor lines. ``info`` only, so they do not change the warn-row contract.

    An engaged policy is not operational. A disengaged policy is not a clearance
    either: the package defaults are simply not this overlay.
    """
    if not policy.engaged:
        return ["info  paper_policy=disengaged"]
    lines = [
        "info  paper_policy=engaged NOT OPERATIONAL entries=blocked approves_no_trade",
    ]
    lines.extend(f"info  paper_policy blocker {blocker}" for blocker in entry_blockers(policy))
    lines.append(
        "info  paper_policy virtual_usdc_equity_unverified engine_sol_default_not_converted"
    )
    return lines


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def operator_overlay_problems(settings: _OverlayView) -> list[str]:
    """Cross-section checks for an engaged policy. Empty when the policy is off.

    Fractions that match the delegated policy are required exactly. Looser
    package defaults fail the load. Exit proposals are rejected by requiring
    the package exit defaults. ``paper_equity_sol`` must stay on the package
    default because this policy does not choose a SOL valuation.
    """
    policy = current_paper_policy_from(settings)
    if not policy.engaged:
        return []
    problems = _posture_problems(settings)
    problems.extend(_risk_problems(settings.risk, policy))
    problems.extend(_limit_problems(settings.limits))
    if settings.exits != ExitsSettings():
        problems.append("paper_policy engaged: exit proposals must stay unapplied")
    if settings.filter != FilterSettings():
        problems.append("paper_policy engaged: filter must stay at package defaults")
    problems.extend(_safety_problems(settings.safety.thresholds))
    return problems


def _posture_problems(settings: _OverlayView) -> list[str]:
    problems: list[str] = []
    if settings.effective_mode != "paper":
        problems.append("paper_policy engaged: effective mode must be paper")
    if settings.live.enabled:
        problems.append("paper_policy engaged: live.enabled must be false")
    if settings.wallet.allowlist:
        problems.append("paper_policy engaged: wallet allowlist must be empty")
    if list(settings.rpc.endpoints) != []:
        problems.append("paper_policy engaged: rpc.endpoints must be empty")
    if settings.rpc.api_key.get_secret_value():
        problems.append("paper_policy engaged: rpc.api_key must be empty")
    if settings.grok.api_key.get_secret_value():
        problems.append("paper_policy engaged: grok.api_key must be empty")
    if settings.notify.allowlist:
        problems.append("paper_policy engaged: notify.allowlist must be empty")
    if settings.health.port != 0:
        problems.append("paper_policy engaged: health.port must be 0")
    return problems


def current_paper_policy_from(settings: _OverlayView) -> PaperDeskPolicy:
    """Read ``paper_policy`` from a settings object without importing Settings."""
    policy = getattr(settings, "paper_policy", None)
    if isinstance(policy, PaperDeskPolicy):
        return policy
    return PaperDeskPolicy()


def _risk_problems(risk: RiskBudget, policy: PaperDeskPolicy) -> list[str]:
    problems: list[str] = []
    expected = {
        "max_trade_fraction": "0.01",
        "max_token_fraction": "0.01",
        "daily_loss_halt_fraction": "0.02",
        "drawdown_halt_fraction": "0.10",
        "partial_fill_fraction": "0.02",
        "paper_equity_sol": "10",
    }
    for name, raw in expected.items():
        if _dec(getattr(risk, name)) != Decimal(raw):
            problems.append(f"paper_policy engaged: risk.{name} must be {raw}")
    if risk.max_open_positions != policy.runtime_position_cap:
        problems.append("paper_policy engaged: risk.max_open_positions must be 3")
    if risk.max_trades_per_day != 10:
        problems.append("paper_policy engaged: risk.max_trades_per_day must be 10")
    if risk.liquidity_floor_multiple != 20:
        problems.append("paper_policy engaged: risk.liquidity_floor_multiple must be 20")
    return problems


def _limit_problems(limits: EngineLimits) -> list[str]:
    problems: list[str] = []
    defaults = EngineLimits()
    if limits.max_slippage_bps != 200:
        problems.append("paper_policy engaged: limits.max_slippage_bps must be 200")
    if limits.min_trade_lamports != defaults.min_trade_lamports:
        problems.append("paper_policy engaged: limits.min_trade_lamports must stay at the default")
    if limits.one_position_per_creator is not True:
        problems.append("paper_policy engaged: limits.one_position_per_creator must stay true")
    if limits.daily_loss_basis != "total":
        problems.append("paper_policy engaged: limits.daily_loss_basis must stay total")
    if limits.blind_max_s != defaults.blind_max_s:
        problems.append("paper_policy engaged: limits.blind_max_s must stay at the default")
    if limits.mtm_interval_s != defaults.mtm_interval_s:
        problems.append("paper_policy engaged: limits.mtm_interval_s must stay at the default")
    if limits.confirm_timeout_s != defaults.confirm_timeout_s:
        problems.append("paper_policy engaged: limits.confirm_timeout_s must stay at the default")
    return problems


def _safety_problems(thresholds: SafetyThresholds) -> list[str]:
    expected = {
        "transfer_tax_hard_fail_pct": "0",
        "transfer_tax_warn_pct": "0",
        "top10_concentration_warn_pct": "20",
        "top10_concentration_fail_pct": "20",
        "liquidity_multiple_min": "20",
        "liquidity_multiple_recommended": "20",
        "lp_lock_min_days": "180",
        "creator_share_veto": "0.10",
        "top5_share_veto": "0.80",
        "kill_below_score": "40",
        "conditional_below_score": "70",
    }
    problems: list[str] = []
    for name, raw in expected.items():
        if _dec(getattr(thresholds, name)) != Decimal(raw):
            problems.append(f"paper_policy engaged: safety.thresholds.{name} must be {raw}")
    return problems
