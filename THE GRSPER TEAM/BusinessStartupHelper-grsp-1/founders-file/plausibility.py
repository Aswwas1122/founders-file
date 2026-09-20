"""
Plausibility review for the LLM-estimated Visualization inputs.

The /estimate endpoint fires eight independent sub-calls. Each one sees the
idea and the sentiment read, but NOT what the other seven returned — so
nothing stops the set from being internally contradictory. Observed failure
shapes this module catches:

  - variable costs at or above starting revenue, i.e. a negative gross margin,
    which makes the business arithmetically incapable of ever profiting
  - a revenue ceiling at or below starting revenue, so market saturation binds
    in month 1 and growth is inert
  - an effective tax rate below 15.3%, which is impossible for a profitable
    sole proprietor since self-employment tax alone is 15.3%
  - a cushion fund below the stop-loss line, which the prompt explicitly says
    it must sit above
  - a growth rate of 15%/month, which compounds to 435%/year
  - high competition intensity paired with aggressive growth, directly
    contradicting the grounding rules the sub-call was given

Two severities:

  ERROR    the value is impossible or makes the simulation meaningless.
           Corrected automatically, and the field is re-tagged "adjusted" so
           the UI stops presenting it as a straight model estimate.
  WARNING  the value is suspicious but defensible. Left alone, surfaced to
           the user.

Nothing here is silent. Every change produces an issue record naming the
field, the original value, the replacement, and why.
"""

from llm_json import as_number

# ---- Bounds. Wide on purpose: this catches nonsense, not unusual businesses. ----
MIN_GROSS_MARGIN = 0.15          # variable costs may not exceed 85% of revenue
MIN_CEILING_MULTIPLE = 1.25      # ceiling must sit at least 25% above month-1 revenue
MAX_CEILING_MULTIPLE = 60.0      # 60x starting revenue is not a small business
MAX_GROWTH_PCT = 25.0            # 25%/mo is already 1,355%/yr
WARN_GROWTH_PCT = 12.0
MIN_GROWTH_PCT = -99.0           # below -100% the (1+g)^n term oscillates
SE_TAX_FLOOR_PCT = 15.3          # self-employment tax alone
MAX_TAX_PCT = 50.0
WARN_TAX_PCT = 45.0
MAX_STEP_MULTIPLE = 3.0          # one step-up may not more than triple fixed costs
WARN_STEP_MULTIPLE = 1.0
MIN_STEP_THRESHOLD_PCT = 10.0
MAX_STEP_THRESHOLD_PCT = 300.0
MAX_COLLECTION_LAG_MONTHS = 3
WARN_BURN_MULTIPLE = 3.0         # starting costs above 3x starting revenue
HIGH_COMPETITION = 70
LOW_DEMAND = 30


def _issue(field, severity, message, original=None, corrected=None):
    return {
        "field": field,
        "severity": severity,
        "message": message,
        "original": original,
        "corrected": corrected,
    }


def review_estimates(fields: dict, sentiment: dict = None) -> tuple:
    """
    Reviews the merged estimate dict in place-safe fashion.

    Returns (corrected_fields, issues, adjusted_field_names).
    """
    f = dict(fields)
    issues = []
    adjusted = set()

    def fix(key, new_value, message):
        issues.append(_issue(key, "error", message, f.get(key), new_value))
        f[key] = new_value
        adjusted.add(key)

    def warn(key, message):
        issues.append(_issue(key, "warning", message, f.get(key)))

    # Coerce everything numeric first so comparisons can't raise.
    numeric = [
        "stopLossThreshold", "cushionFundAmount", "costPerService", "costPerMonth",
        "revenueCeiling", "fixedExpensesMonthly", "fixedExpenseStepThresholdPct",
        "fixedExpenseStepAmount", "variableExpensesMonthly", "growthRatePct",
        "estimatedTaxRatePct", "oneTimeStartupCost", "collectionLagMonths",
    ]
    for key in numeric:
        if key in f:
            f[key] = as_number(f.get(key), 0)

    revenue = f.get("costPerMonth", 0)

    # ---- 1. Non-negative where non-negativity is physical ----
    for key in ("costPerMonth", "costPerService", "revenueCeiling", "fixedExpensesMonthly",
                "variableExpensesMonthly", "oneTimeStartupCost", "cushionFundAmount",
                "fixedExpenseStepAmount", "collectionLagMonths"):
        if key in f and f[key] < 0:
            fix(key, abs(f[key]), f"{key} cannot be negative.")

    # ---- 2. Gross margin must be positive ----
    # Variable costs at or above revenue means the business loses money on
    # every unit sold. No growth rate or cost control can fix that.
    if revenue > 0 and f.get("variableExpensesMonthly", 0) > revenue * (1 - MIN_GROSS_MARGIN):
        capped = round(revenue * (1 - MIN_GROSS_MARGIN))
        fix("variableExpensesMonthly", capped,
            f"Variable costs of ${f.get('variableExpensesMonthly'):,.0f} against ${revenue:,.0f} "
            f"of revenue implies a gross margin at or below {MIN_GROSS_MARGIN:.0%} — the business "
            f"loses money on every sale. Capped to leave a {MIN_GROSS_MARGIN:.0%} gross margin.")

    # ---- 3. Revenue ceiling must leave room to grow ----
    ceiling = f.get("revenueCeiling", 0)
    if revenue > 0:
        if ceiling <= revenue * MIN_CEILING_MULTIPLE:
            fix("revenueCeiling", round(revenue * 3),
                f"A ceiling of ${ceiling:,.0f} at or barely above ${revenue:,.0f} of starting "
                f"revenue means saturation binds immediately and the growth rate does nothing. "
                f"Raised to 3x starting revenue.")
        elif ceiling > revenue * MAX_CEILING_MULTIPLE:
            warn("revenueCeiling",
                 f"A ceiling of ${ceiling:,.0f} is {ceiling / revenue:.0f}x starting revenue. "
                 f"Plausible for a scalable product, but unusual for a small local business.")

    # ---- 4. Growth rate ----
    growth = f.get("growthRatePct", 0)
    if growth > MAX_GROWTH_PCT:
        fix("growthRatePct", MAX_GROWTH_PCT,
            f"{growth}%/month compounds to {((1 + growth / 100) ** 12 - 1) * 100:,.0f}%/year. "
            f"Capped at {MAX_GROWTH_PCT}%/month.")
    elif growth > WARN_GROWTH_PCT:
        warn("growthRatePct",
             f"{growth}%/month is {((1 + growth / 100) ** 12 - 1) * 100:,.0f}%/year — sustainable "
             f"only in an early, uncontested market.")
    if growth < MIN_GROWTH_PCT:
        fix("growthRatePct", MIN_GROWTH_PCT,
            f"A decline steeper than -100%/month is not possible.")

    # ---- 5. Tax rate ----
    tax = f.get("estimatedTaxRatePct", 0)
    if 0 < tax < SE_TAX_FLOOR_PCT:
        fix("estimatedTaxRatePct", SE_TAX_FLOOR_PCT,
            f"{tax}% is below the {SE_TAX_FLOOR_PCT}% self-employment tax that applies before "
            f"any income tax. Raised to the floor.")
    elif tax > MAX_TAX_PCT:
        fix("estimatedTaxRatePct", MAX_TAX_PCT,
            f"{tax}% exceeds a plausible combined effective rate for a sole proprietor.")
    elif tax > WARN_TAX_PCT:
        warn("estimatedTaxRatePct",
             f"{tax}% is high for a sole proprietor unless this is a high-income, high-tax-state case.")

    # ---- 6. Safety net ordering ----
    stop_loss = f.get("stopLossThreshold", 0)
    cushion = f.get("cushionFundAmount", 0)
    if stop_loss > 0:
        fix("stopLossThreshold", -abs(stop_loss),
            f"The stop-loss is a cash floor you stop at, so it should be zero or negative. "
            f"Sign flipped.")
        stop_loss = f["stopLossThreshold"]
    if cushion <= stop_loss:
        fix("cushionFundAmount", round(abs(stop_loss) + 1000),
            f"A cushion of ${cushion:,.0f} sits at or below the ${stop_loss:,.0f} stop-loss line. "
            f"An emergency reserve has to be above the line it's meant to keep you off.")

    # ---- 7. Fixed-cost step-ups ----
    fixed = f.get("fixedExpensesMonthly", 0)
    step = f.get("fixedExpenseStepAmount", 0)
    if fixed > 0 and step > fixed * MAX_STEP_MULTIPLE:
        fix("fixedExpenseStepAmount", round(fixed),
            f"A single step-up of ${step:,.0f} against ${fixed:,.0f} of fixed costs more than "
            f"quadruples them in one jump. Capped at one doubling per step.")
    elif fixed > 0 and step > fixed * WARN_STEP_MULTIPLE:
        warn("fixedExpenseStepAmount",
             f"Each step-up more than doubles fixed costs (${step:,.0f} on top of ${fixed:,.0f}).")

    threshold = f.get("fixedExpenseStepThresholdPct", 0)
    if threshold and not (MIN_STEP_THRESHOLD_PCT <= threshold <= MAX_STEP_THRESHOLD_PCT):
        clamped = min(MAX_STEP_THRESHOLD_PCT, max(MIN_STEP_THRESHOLD_PCT, threshold))
        fix("fixedExpenseStepThresholdPct", clamped,
            f"A step threshold of {threshold}% is outside the usable "
            f"{MIN_STEP_THRESHOLD_PCT:.0f}-{MAX_STEP_THRESHOLD_PCT:.0f}% range.")

    # ---- 8. Collection lag ----
    # Round FIRST, then clamp, and re-read between the two. Clamping first
    # left `lag` holding the pre-fix value, so a 6.4 was clamped to 3 and then
    # "rounded" back up to 6 — a correction that made things worse.
    lag = f.get("collectionLagMonths", 0)
    if lag != round(lag):
        fix("collectionLagMonths", round(lag), "Collection lag is modelled in whole months.")
        lag = f["collectionLagMonths"]
    if lag > MAX_COLLECTION_LAG_MONTHS:
        fix("collectionLagMonths", MAX_COLLECTION_LAG_MONTHS,
            f"A {lag:.0f}-month collection lag is beyond net-90 terms.")

    # ---- 9. Unit price against monthly revenue ----
    unit = f.get("costPerService", 0)
    if revenue > 0 and unit > revenue:
        warn("costPerService",
             f"A ${unit:,.0f} unit price against ${revenue:,.0f} of monthly revenue implies fewer "
             f"than one sale per month. Fine for high-ticket work, otherwise one of the two is off.")

    # ---- 10. Does it burn from day one ----
    burn = fixed + f.get("variableExpensesMonthly", 0)
    if revenue > 0 and burn > revenue * WARN_BURN_MULTIPLE:
        warn("fixedExpensesMonthly",
             f"Starting costs of ${burn:,.0f}/month against ${revenue:,.0f} of revenue means the "
             f"business burns from month 1 and depends entirely on growth outrunning the burn.")

    # ---- 11. Consistency with the sentiment read ----
    # The sub-calls were told to respect these relationships but can't see
    # each other, so nothing enforces it.
    if sentiment:
        breakdown = sentiment.get("scoreBreakdown") or {}
        competition = as_number(breakdown.get("competitionIntensity"), 50)
        demand = as_number(breakdown.get("demandScore"), 50)

        if competition > HIGH_COMPETITION and f.get("growthRatePct", 0) > 8:
            warn("growthRatePct",
                 f"Competition intensity is {competition:.0f}/100 but growth is set to "
                 f"{f.get('growthRatePct')}%/month. The grounding rules call for the opposite — "
                 f"a contested market should slow growth, not accelerate it.")

        if demand < LOW_DEMAND and revenue > 0 and ceiling > revenue * 10:
            warn("revenueCeiling",
                 f"Demand scored {demand:.0f}/100 but the ceiling is {ceiling / revenue:.0f}x "
                 f"starting revenue. A weak demand read should compress the ceiling.")

        band = _parse_cost_band(sentiment.get("startupCost"))
        startup = f.get("oneTimeStartupCost", 0)
        if band and startup > 0:
            low, high = band
            if startup < low / 2 or startup > high * 2:
                warn("oneTimeStartupCost",
                     f"${startup:,.0f} sits well outside the "
                     f"\"{sentiment.get('startupCost')}\" band from the sentiment read, which the "
                     f"estimate was told to anchor on.")

    return f, issues, sorted(adjusted)


def _parse_cost_band(text):
    """Pulls a (low, high) dollar range out of phrasing like 'under $2,000' or '$10,000-$30,000'."""
    if not text or not isinstance(text, str):
        return None
    import re
    nums = [float(n.replace(",", "")) for n in re.findall(r"\$?\s*([\d,]+(?:\.\d+)?)", text)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return None
    lowered = text.lower()
    if len(nums) == 1:
        if "under" in lowered or "less than" in lowered or "below" in lowered:
            return (0.0, nums[0])
        if "over" in lowered or "more than" in lowered or "above" in lowered:
            return (nums[0], nums[0] * 5)
        return (nums[0] * 0.5, nums[0] * 1.5)
    return (min(nums), max(nums))


def summarize(issues: list) -> str:
    """One-line human summary for the panel."""
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    if not issues:
        return ""
    parts = []
    if errors:
        parts.append(f"{len(errors)} estimate{'s' if len(errors) != 1 else ''} "
                     f"had to be corrected to keep the simulation coherent")
    if warnings:
        parts.append(f"{len(warnings)} look{'' if len(warnings) != 1 else 's'} unusual "
                     f"and {'are' if len(warnings) != 1 else 'is'} worth a second look")
    return "; ".join(parts) + "."