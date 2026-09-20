"""
Business > Visualization — input panel backend.

Kept separate from app.py so this feature's routes, prompts, and logic don't
clutter the existing Sentiment/Model/Tax endpoints. Registered onto the main
Flask app as a Blueprint.

Hard dependency (per spec): Visualization requires Sentiment Score to have
already run. The frontend enforces the gate; this module just expects
sentiment context to be passed in when available and works reasonably
without it too.

The eight estimate sub-calls run concurrently and none of them can see what
the others returned, so nothing stops the set from being internally
contradictory — a negative gross margin, a revenue ceiling below starting
revenue, a tax rate under the self-employment floor. Every estimate therefore
passes through plausibility.review_estimates() before it reaches the browser:
impossible values are corrected and re-tagged "adjusted", suspicious ones are
flagged, and both are reported rather than silently applied.
"""

import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, request, jsonify

from config import client, logger, MODEL
from llm_json import extract_json, as_number
from plausibility import review_estimates, summarize

viz_bp = Blueprint("visualization", __name__, url_prefix="/api/visualization")


# ---- Shared grounding rules every parallel sub-call gets, so each narrow
# call still reasons consistently even though they run independently. ----
GROUNDING_RULES = """You will be given the ORIGINAL business idea description (from Stage 1) and the FINAL sentiment analysis results for that exact idea (score, verdict, demand/competition/timing/moat sub-scores, positives, risks). You MUST ground your number in these specific inputs, not in generic industry averages:
- A low moat score (weak defensibility) should push toward lower/more conservative numbers where relevant — an easily-copied business saturates faster and grows less predictably.
- High competitionIntensity should push toward more conservative growth/ceiling numbers.
- A low demandScore should push toward lower cost/revenue numbers and a lower stop-loss threshold.
- Use the sentiment result's own startupCost and revenueIdea as anchors — don't contradict them without reason."""


def _ask_claude_json(system_prompt: str, user_content: str, max_tokens: int = 2500) -> dict:
    """Calls the API and parses a strict-JSON reply.

    Uses the shared client from config.py. It used to do `from app import
    client, MODEL` inside this function, which under `python app.py` imported
    app.py a SECOND time as a separate module — two Flask apps, two Anthropic
    clients, the blueprint registered twice.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        return extract_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"Response was truncated at max_tokens={max_tokens} before it finished "
                f"valid JSON ({len(text)} chars received). Raise max_tokens for this call."
            ) from exc
        raise RuntimeError(f"Model did not return usable JSON: {text[:300]!r}") from exc


def _build_context(idea: str, sentiment: dict) -> str:
    context_lines = [f"ORIGINAL BUSINESS IDEA (Stage 1): {idea}"]

    if sentiment:
        breakdown = sentiment.get("scoreBreakdown", {}) or {}
        positives = sentiment.get("positives") or []
        risks = sentiment.get("risks") or []

        context_lines.append(
            "\nFINAL SENTIMENT ANALYSIS RESULT for this exact idea:\n"
            f"- Overall score: {sentiment.get('score')}/100 — verdict: \"{sentiment.get('verdict')}\"\n"
            f"- Summary: {sentiment.get('summary', '')}\n"
            f"- Demand: {breakdown.get('demandScore')}/100\n"
            f"- Competition intensity: {breakdown.get('competitionIntensity')}/100\n"
            f"- Timing: {breakdown.get('timingScore')}/100\n"
            f"- Moat/defensibility: {breakdown.get('moatScore')}/100\n"
            f"- Likely customer: {sentiment.get('customer', '')}\n"
            f"- Revenue idea: {sentiment.get('revenueIdea', '')}\n"
            f"- Startup cost estimate: {sentiment.get('startupCost', '')}\n"
            f"- Positives noted: {'; '.join(positives) if positives else 'none recorded'}\n"
            f"- Risks noted: {'; '.join(risks) if risks else 'none recorded'}"
        )
    else:
        context_lines.append(
            "\nNo sentiment analysis result was provided — base estimates on the "
            "idea description alone, and be more conservative given the lack of "
            "a validated demand/competition/moat read."
        )

    return "\n".join(context_lines)


# ---- Parallel sub-calls for /estimate ----
# Each entry: key -> (system_prompt, max_tokens). All fire concurrently in ONE
# round, so total latency is bounded by the slowest single call rather than the
# sum of all of them.
ESTIMATE_SUB_CALLS = {
    "sector": (
        f"{GROUNDING_RULES}\nYou judge ONE thing: the sector/category for this business. "
        'Respond ONLY with strict JSON: {"sector": "<short sector name, e.g. \'Food & Beverage\' or \'SaaS\'>"}.',
        120,
    ),
    "safetyNet": (
        f"{GROUNDING_RULES}\nYou judge TWO related things: a stop-loss cash floor and an emergency cushion fund. "
        'Respond ONLY with strict JSON: {"stopLossThreshold": <integer dollars, ZERO OR NEGATIVE — the cash floor at which the founder stops>, '
        '"cushionFundAmount": <integer dollars, POSITIVE emergency reserve, above the stop-loss line>}. '
        "A low demandScore should push the stop-loss threshold less negative (less room to burn cash on unproven demand).",
        180,
    ),
    "pricing": (
        f"{GROUNDING_RULES}\nYou judge TWO related things: price per unit and resulting monthly revenue. "
        'Respond ONLY with strict JSON: {"costPerService": <number, price per unit/instance>, '
        '"costPerMonth": <integer dollars, realistic starting monthly revenue derived from that price and a reasonable volume>}. '
        "costPerMonth should normally be a multiple of costPerService — if it isn't, you are implying fewer than one sale per month.",
        180,
    ),
    "revenueCeiling": (
        f"{GROUNDING_RULES}\nYou judge ONE thing: the realistic maximum monthly revenue this business could reach once it saturates its market at this scale. "
        'Respond ONLY with strict JSON: {"revenueCeiling": <integer dollars>}. '
        "This MUST be meaningfully higher than a plausible starting monthly revenue — a ceiling at or below the "
        "starting figure makes the growth rate meaningless. Low moat + high competition should push this DOWN; "
        "strong moat + low competition can push it higher.",
        150,
    ),
    "fixedExpenses": (
        f"{GROUNDING_RULES}\nYou judge THREE related things about fixed costs (rent, subscriptions, salaries — costs that don't change month to month at the starting scale, but step up at growth milestones). "
        'Respond ONLY with strict JSON: {"fixedExpensesMonthly": <integer dollars, starting fixed cost>, '
        '"fixedExpenseStepThresholdPct": <number between 10 and 300, e.g. 50 means costs step up every time revenue grows 50% past its starting level>, '
        '"fixedExpenseStepAmount": <integer dollars, how much costs jump per step, e.g. hiring help or a bigger space — should not exceed the starting fixed cost>}.',
        250,
    ),
    "variableExpenses": (
        f"{GROUNDING_RULES}\nYou judge ONE thing: total monthly variable costs at the estimated starting volume (materials, contractors, per-unit costs). "
        'Respond ONLY with strict JSON: {"variableExpensesMonthly": <integer dollars>}. '
        "This must leave a positive gross margin — variable costs at or above monthly revenue would mean the business loses money on every sale.",
        120,
    ),
    "growthRate": (
        f"{GROUNDING_RULES}\nYou judge ONE thing: realistic monthly compounding growth rate percentage for this business's revenue/volume — e.g. 3.5 means 3.5% per month, not per year. "
        'Respond ONLY with strict JSON: {"growthRatePct": <number>}. '
        "Remember this compounds: 10%/month is over 200%/year. High competitionIntensity or poor timing should push this "
        "toward a lower, more conservative rate.",
        120,
    ),
    "taxRate": (
        f"{GROUNDING_RULES}\nYou judge ONE thing: a reasonable effective tax rate percentage for a small business/sole proprietor of this kind, accounting for self-employment tax. "
        'Respond ONLY with strict JSON: {"estimatedTaxRatePct": <number>}. '
        "Self-employment tax alone is 15.3%, so the combined effective rate cannot be below that.",
        120,
    ),
    # ---- NEW in v5: the simulation now actually spends startup cash and
    # models a collection lag, so both need estimating. ----
    "startupCost": (
        f"{GROUNDING_RULES}\nYou judge ONE thing: the total ONE-TIME cost to open the doors — equipment, deposits, "
        "licences, initial inventory, branding. NOT recurring monthly costs. "
        'Respond ONLY with strict JSON: {"oneTimeStartupCost": <integer dollars>}. '
        "Anchor on the sentiment result's own startupCost band.",
        150,
    ),
    "collectionLag": (
        f"{GROUNDING_RULES}\nYou judge ONE thing: how long after doing the work this business typically waits to be "
        "paid, in whole months. 0 = paid at the point of sale (retail, food, consumer services). 1 = net-30 "
        "invoicing. 2 = net-60, common for B2B, wholesale and government work. 3 = net-90. "
        'Respond ONLY with strict JSON: {"collectionLagMonths": <integer 0-3>}.',
        120,
    ),
}

ESTIMATE_FIELD_KEYS = [
    "sector", "stopLossThreshold", "cushionFundAmount", "costPerService", "costPerMonth",
    "revenueCeiling", "fixedExpensesMonthly", "fixedExpenseStepThresholdPct",
    "fixedExpenseStepAmount", "variableExpensesMonthly", "growthRatePct", "estimatedTaxRatePct",
    "oneTimeStartupCost", "collectionLagMonths",
]

# Every key the frontend expects, with a usable fallback. A key missing from a
# failed sub-call, or returned as an explicit null, lands on these.
ESTIMATE_DEFAULTS = {
    "sector": "General",
    "stopLossThreshold": -2000,
    "cushionFundAmount": 1500,
    "costPerService": 50,
    "costPerMonth": 1000,
    "revenueCeiling": 5000,
    "fixedExpensesMonthly": 800,
    "fixedExpenseStepThresholdPct": 50,
    "fixedExpenseStepAmount": 300,
    "variableExpensesMonthly": 200,
    "growthRatePct": 2,
    "estimatedTaxRatePct": 20,
    "oneTimeStartupCost": 2000,
    "collectionLagMonths": 0,
}

# Everything except the sector name is a number the simulation does arithmetic
# on, so it gets coerced rather than trusted.
ESTIMATE_NUMERIC_KEYS = set(ESTIMATE_DEFAULTS) - {"sector"}


def _fetch_estimate_fields_parallel(idea: str, sentiment: dict) -> tuple:
    """Fires every ESTIMATE_SUB_CALLS entry concurrently and merges the results
    into one flat dict. A failed individual call leaves its own fields missing
    (filled with defaults later) rather than failing the whole estimate.

    Returns (merged_fields, errors).
    """
    context = _build_context(idea, sentiment)
    merged = {}
    errors = {}

    with ThreadPoolExecutor(max_workers=len(ESTIMATE_SUB_CALLS)) as pool:
        futures = {
            pool.submit(_ask_claude_json, prompt, context, max_tok): key
            for key, (prompt, max_tok) in ESTIMATE_SUB_CALLS.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                data = future.result()
                if isinstance(data, dict):
                    merged.update(data)
                else:
                    errors[key] = f"Expected a JSON object, got {type(data).__name__}"
            except Exception as exc:  # noqa: BLE001
                errors[key] = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "Visualization estimate sub-call '%s' failed — falling back to a default value.\n%s",
                    key, traceback.format_exc(),
                )

    return merged, errors


def _normalize_fields(fields: dict) -> tuple:
    """Fills defaults, coerces numerics, and records where each value came from.

    Returns (fields, origin).

    Two things this fixes:
      - dict.setdefault() only fills an ABSENT key. A sub-call that returned
        valid JSON with an explicit null (e.g. {"revenueCeiling": null}) left
        the key present with value None, so the null survived to the frontend
        and got tagged as a real model estimate. Missing and None are treated
        the same here.
      - Nothing used to coerce types. A model returning "5,000" crashed
        _build_reasoning's format spec and 500'd this endpoint.
    """
    origin = {}

    for key, default_val in ESTIMATE_DEFAULTS.items():
        raw = fields.get(key)

        if raw is None:
            fields[key] = default_val
            # A hardcoded fallback is NOT a model estimate. Saying so stops
            # the UI showing it in the yellow "LLM estimate" colour.
            origin[key] = "default"
        elif key in ESTIMATE_NUMERIC_KEYS:
            coerced = as_number(raw, default_val)
            fields[key] = coerced
            origin[key] = "llm"
        else:
            fields[key] = str(raw).strip() or default_val
            origin[key] = "llm"

    return fields, origin


def _build_reasoning(idea: str, sentiment: dict, fields: dict) -> str:
    """Built deterministically in Python rather than via an extra LLM call —
    it just references the actual sentiment sub-scores and the numbers that
    came back, so it costs no latency."""
    if not sentiment:
        return ("No sentiment data was available, so these are conservative baseline "
                "estimates from the idea description alone.")

    breakdown = sentiment.get("scoreBreakdown", {}) or {}
    risks = sentiment.get("risks") or []
    top_risk = risks[0] if risks else None

    parts = [
        f"Grounded in the {sentiment.get('score')}/100 sentiment score "
        f"(demand {breakdown.get('demandScore')}/100, competition {breakdown.get('competitionIntensity')}/100, "
        f"moat {breakdown.get('moatScore')}/100)."
    ]
    if top_risk:
        parts.append(f"Notably factors in the risk: \"{top_risk}\".")

    # as_number, not the raw value: a model returning "5,000" used to crash the
    # ",.0f" format spec and take the whole endpoint down with a 500.
    parts.append(
        f"Revenue ceiling (${as_number(fields.get('revenueCeiling')):,.0f}) and growth rate "
        f"({as_number(fields.get('growthRatePct'))}%/mo) reflect that competitive/defensibility picture."
    )

    lag = as_number(fields.get("collectionLagMonths"))
    if lag > 0:
        parts.append(
            f"Assumes roughly net-{int(lag) * 30} payment terms, so cash arrives "
            f"{int(lag)} month{'s' if lag != 1 else ''} after the work is done."
        )

    return " ".join(parts)


@viz_bp.route("/estimate", methods=["POST"])
def estimate():
    """
    Auto-fill the Visualization input panel's LLM-predicted fields.
    Expects: { "idea": "...", "sentiment": {...prior Sentiment Score result...} }

    All sub-estimates run in parallel, then the merged set is normalized
    (defaults, type coercion, origin tagging) and reviewed for internal
    consistency before being returned.
    """
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    sentiment = data.get("sentiment") or {}

    if not idea:
        return jsonify({"error": "Missing idea"}), 400

    try:
        fields, sub_errors = _fetch_estimate_fields_parallel(idea, sentiment)
        fields, origin = _normalize_fields(fields)

        # Cross-check the sub-calls against each other and against the
        # sentiment read. Impossible values are corrected; suspicious ones are
        # flagged. Nothing is changed silently.
        fields, issues, adjusted = review_estimates(fields, sentiment)
        for key in adjusted:
            origin[key] = "adjusted"

        result = {key: fields[key] for key in ESTIMATE_FIELD_KEYS}
        result["reasoning"] = _build_reasoning(idea, sentiment, result)
        result["origin"] = origin
        result["plausibility"] = {
            "issues": issues,
            "summary": summarize(issues),
        }

        if sub_errors:
            total = len(ESTIMATE_SUB_CALLS)
            failed = len(sub_errors)
            result["_warning"] = (
                f"{failed} of {total} estimate calls failed and fell back to neutral defaults. "
                "See the terminal running this server for the exact error."
            )
            result["_errorDetail"] = sub_errors

        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("estimate() failed outright:\n%s", traceback.format_exc())
        return jsonify({"error": str(exc)}), 500


def _amortized_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    """Standard amortization formula: payment = P * r / (1 - (1+r)^-n)."""
    monthly_rate = (annual_rate_pct / 100) / 12
    if monthly_rate == 0:
        return principal / term_months
    return principal * monthly_rate / (1 - (1 + monthly_rate) ** (-term_months))


# ---- Fixed interest rates by loan type ----
# Deliberately NOT LLM-guessed — fixed, deterministic assumptions per loan type
# so the rate is predictable every time. Bank loans use a standard small-
# business term rate; brokerage (margin-style) loans carry a meaningfully
# higher one.
FIXED_LOAN_RATES_PCT = {
    "bank": 7.5,
    "brokerage": 11.0,
}

MIN_LOAN_TERM_MONTHS = 6
MAX_LOAN_TERM_MONTHS = 300


@viz_bp.route("/loan-terms", methods=["POST"])
def loan_terms():
    """
    Determine loan terms once a loan amount + type is entered.
    Expects: { "loanAmount": number, "loanType": "bank" | "brokerage" }

    The interest rate is fixed per loan type. Only the repayment term comes
    from the model, and the monthly payment is computed exactly via
    amortization rather than guessed.
    """
    data = request.get_json(force=True)
    loan_amount = as_number(data.get("loanAmount"), 0)
    loan_type = (data.get("loanType") or "").strip().lower()

    if loan_amount <= 0:
        return jsonify({"error": "loanAmount must be greater than 0"}), 400
    if loan_type not in FIXED_LOAN_RATES_PCT:
        return jsonify({"error": "loanType must be 'bank' or 'brokerage'"}), 400

    interest_rate_pct = FIXED_LOAN_RATES_PCT[loan_type]

    system_prompt = """You suggest a realistic repayment term for a small business loan. Respond ONLY with strict JSON, no markdown fences:
{
  "termMonths": <integer, realistic repayment term in months for this loan type and amount, e.g. 36 or 60>,
  "taxNote": "<1 sentence on how this loan type is typically treated for tax purposes>"
}"""
    user_content = (
        f"Loan amount: ${loan_amount:,.0f}. Loan type: {loan_type} loan. "
        f"Fixed annual interest rate: {interest_rate_pct}%."
    )

    try:
        result = _ask_claude_json(system_prompt, user_content, max_tokens=150)

        # Coerce and bound the term: a model returning "60 months", 0, or 1200
        # would otherwise divide by zero or produce a meaningless payment.
        term_months = int(as_number(result.get("termMonths"), 60)) or 60
        term_months = max(MIN_LOAN_TERM_MONTHS, min(MAX_LOAN_TERM_MONTHS, term_months))

        monthly_payment = _amortized_payment(loan_amount, interest_rate_pct, term_months)

        result["termMonths"] = term_months
        result["interestRatePct"] = interest_rate_pct
        result["monthlyPayment"] = round(monthly_payment, 2)
        result["totalInterest"] = round(monthly_payment * term_months - loan_amount, 2)
        result["origin"] = {
            "interestRatePct": "fixed",    # deterministic by loan type
            "termMonths": "llm",
            "monthlyPayment": "computed",  # derived via amortization
            "totalInterest": "computed",
        }
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("loan_terms() failed:\n%s", traceback.format_exc())
        return jsonify({"error": str(exc)}), 500


@viz_bp.route("/suggest-stream", methods=["POST"])
def suggest_stream():
    """
    Suggests ONE additional plausible revenue stream, given the idea and
    whatever streams already exist so it doesn't repeat the primary one.
    Expects: { "idea": "...", "existingStreams": [{"name": "..."}] }
    """
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    existing = data.get("existingStreams") or []

    if not idea:
        return jsonify({"error": "Missing idea"}), 400

    existing_names = ", ".join(
        str(s.get("name", "")) for s in existing if isinstance(s, dict) and s.get("name")
    ) or "none yet"

    system_prompt = """You suggest ONE additional, genuinely plausible revenue stream for a small business, distinct from streams that already exist. Respond ONLY with strict JSON, no markdown fences:
{
  "name": "<short stream name, e.g. 'Wholesale accounts' or 'Premium tier'>",
  "monthlyAmount": <integer dollars, realistic starting monthly revenue for this NEW stream specifically, modest since it's just starting>,
  "growthRatePct": <number, realistic monthly growth rate percentage for this stream, between 0 and 15>,
  "reasoning": "<1 sentence on why this is a plausible additional stream for this business>"
}
Do not suggest a stream that duplicates or trivially rewords an existing one."""
    user_content = f"Business idea: {idea}. Existing revenue streams: {existing_names}."

    try:
        result = _ask_claude_json(system_prompt, user_content, max_tokens=200)

        # Coerce before it reaches the stream list — the frontend does
        # arithmetic on these, and a string amount used to flow straight into
        # the simulation.
        result["name"] = str(result.get("name") or "Suggested stream").strip()
        result["monthlyAmount"] = max(0.0, as_number(result.get("monthlyAmount"), 0))
        result["growthRatePct"] = max(-99.0, min(25.0, as_number(result.get("growthRatePct"), 0)))
        result["origin"] = "llm"
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("suggest_stream() failed:\n%s", traceback.format_exc())
        return jsonify({"error": str(exc)}), 500