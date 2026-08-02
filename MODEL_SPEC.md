# Frozen research specification

## 1. Time standard

All trading rules are defined in `America/New_York`, not a fixed UK clock time. This prevents spring/autumn daylight-saving mismatches from silently shifting the US-market entry.

Alpaca timestamps are read as UTC and converted to New York time. The Alpaca market calendar supplies each date's actual regular-session open and close, including early-close sessions.

## 2. Candidate availability

A candidate must exist before the research cutoff.

For live/imported scanner alerts, the app inspects every available audit timestamp among `cutoff_at`, `decision_at` and `created_at`. It uses the latest timestamp as the moment the candidate was actually knowable. That timestamp must be no later than the research cutoff.

Rows explicitly marked `decision='calibration'` are historical point-in-time reconstructions, so their modern database `created_at` is an ingestion timestamp rather than a historical availability timestamp. For those rows, the app uses the logical cutoff/decision timestamp. If neither exists, it reconstructs the scanner's frozen 17:00 Europe/London cutoff with the correct historical daylight-saving conversion.

Duplicate scanner rows for the same symbol and date are collapsed to the earliest valid candidate. Explicit scanner `reject` rows and rows explicitly marked security-ineligible are excluded; null legacy eligibility is retained for audit rather than silently discarded. If no auditable or defensible logical availability time exists, scanner-alert mode fails or excludes the row rather than assuming it was known.

This is especially important during the short weeks when 17:00 London is 13:00 ET rather than 12:00 ET.

Manual symbols are predeclared before collection. Uploaded CSV candidates are treated as externally preselected; their provenance remains the user's responsibility.

## 3. Signal cutoff

Default cutoff: 12:30 ET.

Only bars timestamped before 12:30 ET are available to the baseline dip gate. A bar timestamped 12:29 represents the 12:29–12:30 interval and is the final bar available at the cutoff.

When the baseline dip gate is active, the exact session-open bar and exact final pre-cutoff bar must exist. Missing point-in-time coverage causes the candidate to be excluded.

## 4. Baseline dip gate

At cutoff:

```text
minimum price                       US$2
maximum price                       US$50
minimum cumulative dollar volume   US$5,000,000
minimum drawdown from morning high  3%
minimum decline from session open   1%
price below cumulative VWAP          required
```

Dollar volume is the sum of one-minute close multiplied by one-minute volume. Cumulative VWAP uses Alpaca bar VWAP where present and volume-weighted typical price otherwise.

The gate may be disabled for pipeline tests or externally selected candidate CSVs. Any run with changed thresholds is a different hypothesis and must retain its own sealed split.

## 5. Pre-registered cutoff-price segments

Every discovery and validation comparison is calculated for:

```text
all candidates
US$2 to below US$5
US$5 to below US$20
US$20 to US$50 inclusive
```

The validation winner freezes both `segment_key` and `variant_key`. Benjamini-Hochberg correction is applied across all tested segment-entry combinations at each split and cost assumption. On sealed dates, candidates outside the frozen winner segment are not traded.

## 6. Fixed-time entries

Default fixed entries:

```text
12:31 ET
12:35 ET
12:45 ET
13:00 ET
```

Entry price is the open of the bar timestamped at exactly the stated time. Missing bars are not forward-filled. A halted stock with no 12:31 bar therefore has no 12:31 trial.

Every fixed entry must be later than the configured signal cutoff.

## 7. Confirmation entries

### Higher low

A completed bar has:

```text
current low > previous low
current close > current open
```

### VWAP reclaim

A completed bar crosses from at/below cumulative VWAP to above cumulative VWAP.

### Prior-bar high

A completed bar closes above the previous bar's high.

### Five-bar break

A completed bar closes above the maximum high of the preceding five completed bars.

For every confirmation, entry occurs at the open of the immediately consecutive one-minute bar. If that minute is absent, the trigger is not treated as executable.

## 8. Outcomes

Default success objective, trigger and stop:

```text
net success objective = +3.0% after tested costs
gross target trigger  = entry × 1.035
stop                  = entry × 0.950
```

The gross trigger must cover the configured net target plus the highest cost stress. With the defaults, a +3.5% target remains +3.0% after the 50-bps stress.

The app evaluates the entry bar and every later regular-session minute.

Ordering:

1. A later bar opening through the stop exits at that worse opening price; the app never assumes a fill back at the stop.
2. A later bar opening above the target counts as a target fill at the target price.
3. If target and stop occur inside the same one-minute bar without an opening gap deciding the order, assume stop first.
4. Otherwise, the first bar to touch either level defines the exit.
5. If neither level is reached, exit at the final expected regular-session bar close.
6. If the expected final bar is absent, a target-or-close trial without an earlier target/stop is excluded because the close is not demonstrably executable.

Cost-adjusted return:

```text
net return percentage = gross return percentage - cost basis points / 100
```

The bar open and fixed cost stress are reproducible proxies, not reconstructed bid/ask fills.

## 9. Chronological splits and sealed workflow

Unique valid trading dates are sorted and allocated:

```text
first 60%   discovery
next 20%    validation
final 20%   sealed test
```

All symbols on the same date remain in the same split. This prevents same-day market conditions leaking across splits.

During the first worker phase:

- discovery and validation candidates are processed;
- all entry variants are compared;
- a validation winner is frozen;
- sealed candidates remain in status `sealed` and have no trials.

After the user selects **Open sealed test**:

- sealed candidates are queued;
- only the frozen validation winner is executed;
- final classification combines validation and sealed evidence;
- sealed data cannot rescue a rule that failed validation.

## 10. Statistical summary

Each entry-and-price-segment variant and cost assumption receives:

- observations, symbols and independent trading dates;
- median and interquartile actual entry time in New York time;
- probability of finishing at or above the configured net target;
- event-level Wilson and date-resampled lower confidence bounds for net-target success;
- rate of net losses of at least 5%;
- mean, median, quartiles and win rate;
- target-before-stop and stop-before-target rates;
- profit factor (reported as 999 when there are positive returns and no losing observations);
- equal-weight daily return series;
- compounded sequence maximum drawdown;
- circular moving-block bootstrap 95% confidence interval over the ordered daily series;
- one-sided bootstrap p-value;
- Benjamini-Hochberg q-value across discovery/validation variants;
- best-symbol profit concentration.

## 11. Winner selection

The app ranks variants using validation results at the highest configured cost assumption.

A pre-sealed `validation_passed_pending_sealed_test` verdict requires, at minimum:

- 100 observations;
- 20 independent dates;
- positive validation mean and median net return, with mean at least 0.5%;
- at least 55% of observations achieving the configured net target;
- event-level net-target lower bound at least 45%;
- date-resampled net-target lower bound at least 40%;
- net loss of at least 5% on no more than 30% of observations;
- positive bootstrap 95% lower bound for mean net return;
- q-value at or below 0.05;
- profit factor at least 1.2;
- no single symbol contributing more than 25% of positive profit.

Final `validated_for_paper_testing` also requires the frozen winner to retain at least 50% net-target success, a date-resampled lower bound of at least 30%, positive confidence-supported mean return, controlled 5% losses and unconcentrated performance in at least 30 sealed observations across at least 10 dates.

## 12. Limitations

- One-minute bars do not reveal the order of high and low inside a bar; the same-bar rule is conservative.
- Entry at a bar open is a reproducible proxy, not a guaranteed fill.
- Cost stress is not a quote-level spread/slippage reconstruction.
- Halts and missing bars can reduce available entry variants.
- Corporate-action handling uses `adjustment=split`; dividends and other actions are not separately modelled.
- A candidate list sourced from current scanner alerts may inherit selection or survivorship bias from that scanner.
- Database administrators can technically inspect database rows; the application-level sealed workflow prevents accidental UI/export leakage but is not a cryptographic vault.
- No result authorises live deployment.
