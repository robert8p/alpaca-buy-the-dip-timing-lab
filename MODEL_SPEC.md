# Model specification

## Research question

Identify a reproducible sequence:

**oversold state → selling exhaustion → reversal confirmation → next-bar entry → sufficient remaining upside**

The goal is not to buy all declines. It is to distinguish a temporary dislocation from continuing deterioration.

## Candidate populations

The app can use:

- `pre_open` scanner alerts;
- `midday` scanner alerts;
- both populations together, deduplicated by symbol and date;
- manual symbols;
- a candidate CSV with an optional point-in-time availability field.

Scanner alerts are candidate generators only. The app independently verifies whether the stock actually becomes oversold.

## Point-in-time oversold state

Default thresholds:

- price between US$2 and US$50;
- cumulative dollar volume of at least US$5 million by the trigger;
- at least 5% below the session high;
- at least 2% below the session open;
- at least 1% below cumulative VWAP;
- oversold state remains eligible for 15 minutes after it occurred;
- at least 30 completed one-minute bars before a trigger can fire.

All features at bar `t` use only bar `t` and earlier data. Entry occurs at the open of bar `t+1`, and only if that exact next minute exists.

## Trigger recipes

1. `deep_higher_low` — deep washout, then first higher-low green bar.
2. `deep_prior_high_break` — deep washout, then close above prior-bar high.
3. `deep_two_green` — deep washout, then two rising green closes.
4. `capitulation_higher_low` — recent volume climax, volume contraction, then higher low.
5. `capitulation_prior_high` — recent volume climax, volume contraction, then prior-high break.
6. `momentum_turn_higher_low` — three-minute downside momentum improves by at least one percentage point, then higher low.
7. `relative_strength_turn` — stock recovers short-term relative strength versus SPY and breaks the prior bar high.
8. `vwap_reclaim_after_deep` — deep washout followed by a VWAP reclaim.
9. `five_bar_break_after_deep` — deep washout followed by a close above the previous five-bar high.

Only the first occurrence of each recipe per symbol-date is tested. This prevents repeated entries from one noisy path inflating the sample.

## Outcome simulation

Default economics:

- net target: 3.0%;
- gross target: 3.5%;
- stop: 5.0%;
- costs: 15, 20 and 50 bps;
- exit: target, stop or official session close.

Conservative path rules:

- exact next-bar open is the entry;
- if target and stop are both inside one minute bar, stop is assumed first;
- a gap below the stop exits at the lower opening price;
- an incomplete final path is not used for a market-close outcome.

## Chronological splits

- discovery: first 60% of independent dates;
- validation: next 20%;
- sealed test: final 20%.

The sealed split is not downloaded or processed until a validation-qualified rule is frozen.

## Material consistency

Strong evidence requires, at the highest configured cost:

- at least 100 observations;
- at least 20 independent dates;
- at least 20 symbols;
- mean net return at least 0.75%;
- median net return at least 0.25%;
- positive lower bootstrap confidence bound;
- net-target success at least 40%;
- Wilson lower bound at least 30%;
- at least 60% positive dates;
- at least 55% positive symbols;
- every chronological fold positive;
- profit factor at least 1.5;
- no more than 25% of observations losing at least 5%;
- limited best-stock and best-date concentration;
- multiple-testing-adjusted q-value no more than 0.05.

Before validation can select a recipe, the matching discovery recipe and price segment must also show positive, diversified and fold-stable performance.

## Exceptional small-sample policy

A small sample is labelled compelling only when it is unusually strong:

- at least 12 observations, 3 dates and 5 symbols;
- mean net return at least 1.5%;
- median at least 0.75%;
- at least 50% achieve the net target;
- every validation date positive;
- at least 70% of symbols positive;
- every fold averages at least 0.5%;
- profit factor at least 2.0;
- limited concentration;
- q-value no more than 0.25.

This label only permits a frozen sealed test on the already-held-out dates. It does not justify extending the date window or deploying capital.
