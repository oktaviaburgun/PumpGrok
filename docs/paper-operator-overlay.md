# Paper operator overlay

Status: NOT OPERATIONAL. This note is an install and verification record for a
fail-closed paper overlay. It approves no trade, no CLEAR, no fill, and no
paper run. A policy pass is not a RISK CLEAR.

The package overlays under `src/pumpgrok/config/overlays/` are unchanged.
Nothing in this file is in force until the desk computer sets `PUMPGROK_CONFIG`
to the example file. A checkout on another machine does not update that computer.

## Fixed inputs

These are the human-fixed values. This change does not re-choose them.

| Input | Value |
|---|---|
| Mode | paper only |
| Starting equity | 1000 virtual USDC |
| Max trade | 10 virtual USDC |
| Position ceiling | 5, not applied |
| Runtime position cap | 3 |
| Daily loss halt | 2 percent of verified day-open equity, Asia/Bangkok, latched until a human clears it |
| Drawdown halt | 10 percent of the running peak, latched |

Exit proposals (stop 20, take-profit 60, trail 20, hold 1800s, poll 15s) are
not applied. Package defaults remain 30, 120, 35, 3600, and 15.

## Readiness

These three answers are separate.

| Question | Shipped overlay |
|---|---|
| Configuration ready | yes, when `PUMPGROK_CONFIG` loads the example |
| Data ready | no |
| Execution ready | no |

Configuration ready means the file validates: paper mode, 1000 virtual USDC,
10 virtual USDC max trade, runtime cap 3, user ceiling 5 recorded, 2 percent
daily loss, 10 percent drawdown, Bangkok day rules, 200 bps cap, strict
safety gate, and an empty cost model that the schema can later fill.

Data ready means a sourced cost model is verified and a ledger E0 and peak
exist. The example leaves `[paper_policy.cost_model] verified = false`.
This change does not write equity or a peak. `initialization_prerequisites()`
in `src/pumpgrok/risk/paper_accounting.py` lists what a later authorized
initialization must record.

Execution ready means an order may be sent. It stays no. `run`, `paper`,
`state`, `killswitch`, `ticket`, `live`, `verify`, `backtest`, and `tools`
are refused. The operator registry is not built. No adapter is constructed.

`pumpgrok doctor` with the health port left at 0 prints
`configuration_ready=yes`, `data_ready=no`, `execution_ready=no`, and
`NOT OPERATIONAL`. Exit 0 means the settings loaded.

## Accounting

`src/pumpgrok/risk/paper_accounting.py` is a pure virtual-USDC book.

- 1 virtual USDC = 1_000_000 micro units. 1000 virtual USDC is
  1_000_000_000 micro units.
- That quantity is a different number from the package default of 10 SOL
  in lamports. No SOL/USDC price is used.
- Asia/Bangkok is UTC+7 with no daylight-saving change. A new Bangkok day
  sets E0 to the equity then observed and resets the daily entry count.
  A latched halt stays latched. `clear_halt` accepts only a human clear.
- Daily loss uses total equity. At 2 percent the book latches `daily_loss`.
  At 10 percent under the running peak it latches `drawdown`.
- An external flow moves cash, day-start, and peak by the same amount, so
  the loss amount is unchanged, and entries stay blocked until the flow is
  marked reconciled.
- All-in entry cost is size plus the verified model's fee, slippage, and
  network cost. Above 10 virtual USDC the entry does not qualify. The size
  is not reduced to fit.
- Any missing, blind, or failed required safety row is KILL. A warning is
  CONDITIONAL and does not qualify.
- `bonding_curve` stage is excluded. Slippage above 200 bps does not qualify.
  A 500 bps stress haircut is a valuation input when a model supplies one.
  It does not raise the 200 bps cap.
- `qualifies` true on a complete in-memory fixture is still
  `execution_ready` false. The function writes nothing.

## Execution boundary

With the policy engaged, `RiskEngine.check` returns before the 19-step walk
and writes no ledger row:

- `blind_flatten_prohibited` for that route
- `slippage_above_cap` when the ticket asks for more than 200 bps
- `execution_not_authorized` for every other order

The paper adapter file is unchanged. The route is refused before an adapter
is built.

## Cost model schema

`[paper_policy.cost_model]` accepts `verified`, `source`, `as_of`,
`venue_fee_rate_per_side`, `network_cost_virtual_usdc_per_side`,
`entry_slippage_bps` (max 200), and `stress_exit_haircut_bps`.
`verified = true` requires every one of those inputs. The example sets
`verified = false` and leaves the inputs empty, so cost evidence remains
a data blocker. A test fixture may pass a complete model whose source is
`TEST DATA synthetic fixture`. That fixture is not venue evidence.

## What stays outside this change

- No wallet, key, or signature.
- No market scan, RPC call, paper fill, or live fill.
- No ledger initialization of E0 or peak.
- Package paper, dev, and live overlays are unchanged.
- The supervisor `DailyRollover` emitter still keys UTC. The Bangkok clock
  lives in the virtual-USDC book. Wiring that emitter is a later step.
- `safety.score_report` still has its advisory BLIND path. The deployment
  gate is `safety_verdict` in the virtual-USDC book.
- `engine/analyzer.py` `CREATOR_SHARE_VETO` remains 0.25.
- Exit proposals stay at package defaults 30, 120, 35, 3600, and 15.
  Applying them is waiting on a desk-agent decision. Leaving them unapplied
  does not by itself reject the overlay.
- Package data, Jupiter, and Grok URLs remain because those fields require
  https URLs. This overlay does not call them.

## Questions for coordinator YJ

- A sourced cost model: source, date, fee rate, network cost, and slippage.
  The schema can store it. The example does not invent one.
- Whether the supervisor UTC rollover should call the Bangkok book clock.
- Whether `safety.score_report` should call `safety_verdict`.
- Whether exit proposals may be applied later.
- Whether the runtime position cap may later rise from 3 toward the ceiling of 5.
- Whether pre-migration tokens get a separate policy. The book excludes them.
- When a human authorizes ledger initialization. The prerequisite list is in
  code. This change does not perform it.

## Desk computer

Do this on the existing desk clone. Pulling or editing a different checkout
does not update that clone.

1. Fetch this branch, or apply its patch, in that clone.
2. From the repo root, install the locked dev environment if it is not already
   present: `uv sync --frozen --group dev`. That command does not start the desk.
3. Export `PUMPGROK_CONFIG` as the absolute path of
   `src/pumpgrok/config/examples/paper-operator.example.toml`.
   Leave `PUMPGROK_PROFILE` unset or set it to `paper`.
   Do not set `PUMPGROK_MODE=live`.
   Do not export a wallet, seed, signing, or provider credential.
   Do not set `PUMPGROK_HEALTH__PORT`.
4. Run the offline checks in the next section. They must not be replaced with
   `pumpgrok run`, `pumpgrok paper`, replay, serve, or a market tool.
5. Optional: `uv run pumpgrok doctor` with the same environment. Expect the
   `NOT OPERATIONAL` line. Do not treat a zero exit as permission to trade.

## Offline checks

From the repo root, with `PUMPGROK_CONFIG` unset for the command below (the
tests set it themselves):

```bash
uv run python -m pytest -o addopts= \
  tests/config/test_paper_operator_overlay.py \
  tests/risk/test_paper_policy_blocks_entries.py \
  tests/risk/test_paper_accounting.py \
  -q
```

These tests load settings, refuse CLI verbs, refuse orders inside
`RiskEngine.check` with an empty ledger, and run the virtual-USDC book on
rows marked TEST DATA. They do not append a fill, call a market or RPC
endpoint, or start a desk cycle.

## Rollback

Unset `PUMPGROK_CONFIG`. The package defaults return. Revert the patch to
remove the gate. No ledger row is required for the rollback because this
change does not write one.
