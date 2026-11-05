"""Operator paper-policy schema and overlay checks.

The engaged policy is denominated in virtual USDC. It does not convert that
amount to SOL and it does not write a ledger. Exit proposals stay unapplied
when the exit block still matches the package defaults; that fact is not
itself an entry blocker.

Execution stays unauthorized in this build. Data readiness is separate and
can become ready when a caller supplies a complete cost model and an
in-memory book. See ``paper_accounting``.
"""

from __future__ import annotations

import contextvars
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class PaperCostModel(BaseModel):
    """Sourced cost inputs. ``verified`` is false unless every input is present.

    A verified model is still not venue evidence unless its source says so.
    The shipped overlay leaves it unverified. Tests may supply a model marked
    as test data by its source text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: bool = False
    source: str = ""
    as_of: str = ""
    venue_fee_rate_per_side: Decimal | None = None
    network_cost_virtual_usdc_per_side: Decimal | None = None
    entry_slippage_bps: int | None = Field(default=None, ge=0, le=10_000)
    stress_exit_haircut_bps: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def _verified_is_complete(self) -> PaperCostModel:
        if not self.verified:
            return self
        missing: list[str] = []
        if not self.source.strip():
            missing.append("source")
        if len(self.as_of) != 10 or self.as_of[4] != "-" or self.as_of[7] != "-":
            missing.append("as_of")
        if self.venue_fee_rate_per_side is None:
            missing.append("venue_fee_rate_per_side")
        if self.network_cost_virtual_usdc_per_side is None:
            missing.append("network_cost_virtual_usdc_per_side")
        if self.entry_slippage_bps is None:
            missing.append("entry_slippage_bps")
        if self.stress_exit_haircut_bps is None:
            missing.append("stress_exit_haircut_bps")
        if missing:
            raise ValueError("verified cost model requires " + ", ".join(missing))
        if self.entry_slippage_bps is not None and self.entry_slippage_bps > 200:
            raise ValueError("entry slippage cannot exceed 200 bps")
        return self


class PaperDeskPolicy(BaseModel):
    """Operator overlay block ``[paper_policy]``.

    ``engaged`` selects the virtual-USDC deployment policy. Fractions below
    are the book's own limits. They are not a SOL valuation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engaged: bool = False
    denomination: str = "unset"
    unit: str = "virtual_usdc_micro"
    starting_equity_virtual_usdc: Decimal | None = None
    max_trade_virtual_usdc: Decimal | None = None
    user_position_ceiling: int = Field(default=5, ge=1, le=5)
    runtime_position_cap: int = Field(default=3, ge=0, le=3)
    halt_timezone: str = "UTC"
    max_slippage_bps: int = Field(default=200, ge=1, le=10_000)
    daily_loss_halt_fraction: Decimal = Decimal("0.02")
    drawdown_halt_fraction: Decimal = Decimal("0.10")
    max_trades_per_day: int = Field(default=10, ge=0)
    blind_is_kill: bool = True
    cost_model: PaperCostModel = Field(default_factory=PaperCostModel)

    @model_validator(mode="after")
    def _fixed_inputs_when_engaged(self) -> PaperDeskPolicy:
        if not self.engaged:
            return self
        expected: tuple[tuple[str, object], ...] = (
            ("denomination", "virtual_usdc"),
            ("unit", "virtual_usdc_micro"),
            ("starting_equity_virtual_usdc", Decimal("1000")),
            ("max_trade_virtual_usdc", Decimal("10")),
            ("user_position_ceiling", 5),
            ("runtime_position_cap", 3),
            ("halt_timezone", "Asia/Bangkok"),
            ("max_slippage_bps", 200),
            ("daily_loss_halt_fraction", Decimal("0.02")),
            ("drawdown_halt_fraction", Decimal("0.10")),
            ("max_trades_per_day", 10),
            ("blind_is_kill", True),
        )
        problems = [name for name, wanted in expected if getattr(self, name) != wanted]
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


def _config_data_blockers(policy: PaperDeskPolicy) -> tuple[str, ...]:
    """Data gaps of the loaded file. An unverified cost model stays a gap."""
    if not policy.engaged:
        return ()
    blockers: list[str] = []
    if not policy.cost_model.verified:
        blockers.append("cost_model_unverified")
    blockers.append("equity_not_initialized")
    blockers.append("market_evidence_not_loaded")
    return tuple(blockers)


def entry_blockers(
    policy: PaperDeskPolicy,
    *,
    side: str = "buy",
    slippage_bps: int | None = None,
    exit_reason: str | None = None,
) -> tuple[str, ...]:
    """Execution-boundary reasons for an engaged policy.

    Data blockers are included for buys. Slippage above the cap and a blind
    flatten route are named on their own. A disengaged policy adds none.
    """
    if not policy.engaged:
        return ()
    if exit_reason == "blind_flatten":
        return ("blind_flatten_prohibited",)
    if slippage_bps is not None and slippage_bps > policy.max_slippage_bps:
        return ("slippage_above_cap",)
    if side != "buy":
        return ("execution_not_authorized",)
    blockers = ["execution_not_authorized"]
    if not policy.cost_model.verified:
        blockers.append("cost_model_unverified")
    blockers.append("equity_not_initialized")
    blockers.append("market_evidence_not_loaded")
    return tuple(blockers)


def paper_policy_report_lines(policy: PaperDeskPolicy) -> list[str]:
    """Doctor lines. ``info`` only, so they do not change the warn-row contract.

    Configuration readiness is reported apart from data readiness and from
    execution readiness. Execution stays unauthorized.
    """
    if not policy.engaged:
        return ["info  paper_policy=disengaged"]
    blockers = _config_data_blockers(policy)
    data_ready = "yes" if not blockers else "no"
    lines = [
        "info  paper_policy configuration_ready=yes",
        f"info  paper_policy data_ready={data_ready}",
        "info  paper_policy execution_ready=no",
        "info  paper_policy NOT OPERATIONAL entries=blocked approves_no_trade",
    ]
    lines.extend(f"info  paper_policy data_blocker {blocker}" for blocker in blockers)
    lines.append("info  paper_policy unit=virtual_usdc_micro separate_from_sol_lamports")
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
