# Stage 6A Market Isolation Contract

Date: 2026-08-20

## Purpose

Stage 6A creates the governance boundary required before expanding beyond the
A-share-first product. It prevents A-share rules, thresholds, models,
calibration and performance from being silently reused for HK Connect or US
markets.

## Market profiles

Each `MarketResearchProfile` binds:

```text
market and access scope
settlement currency
time zone
prediction horizons
configuration
calendar snapshot
universe snapshot
market-rule identity
cost-schedule identity
data snapshot
feature policy
label policy
model identity
calibration identity
Strategy Scoreboard identity
trust, verification and completeness
```

Supported access scopes are:

```text
A_DOMESTIC
HK_CONNECT
HK_BROAD
US_CASH
```

Scope, market, currency and time zone must agree. Horizons must be explicit,
positive, sorted and unique.

## Isolation bundle

The default expansion bundle requires:

```text
A_DOMESTIC
HK_CONNECT
US_CASH
```

The following identities must be independent across profiles:

- configuration;
- calendar;
- universe;
- market rules;
- cost schedule;
- data snapshot;
- feature policy;
- label policy;
- model;
- calibration;
- Scoreboard.

Any shared identity produces a blocker. Missing, incomplete, unverified or
low-trust profiles also block formal isolation. Synthetic profiles keep the
bundle `DIAGNOSTIC_ONLY`.

## Cross-market transfer

Cross-market feature, label, model, threshold, calibration or strategy-rule
reuse is forbidden by default. A transfer request may reach only
`SHADOW_ONLY` when it has:

- explicit approval evidence;
- target-market validation evidence;
- `shadow_only=true`;
- production weight zero;
- no created orders;
- complete and verified source and target profiles.

Even a valid request always reports:

```text
allows_production_reuse = false
deploys_model = false
changes_runtime_weight = false
creates_order = false
```

A same-market HK Connect to broad-HK change is not represented as a
cross-market transfer; it requires its own access-scope review.

## Current implementation status

The isolation and Shadow-transfer contracts are implemented and tested. This
does not mean HK Connect or US support is live. The following remain future
work:

- authoritative historical universes and delistings;
- trading calendars and trading-status history;
- corporate actions and adjustment conventions;
- market-specific fees, settlement, lots and price limits;
- real providers and health monitoring;
- PIT research data;
- market-specific models and calibration;
- independent real Strategy Scoreboards and Shadow validation.

The project remains `LICENSE_PENDING` and `T3_NOT_REACHED`.
