"""
Sentiment composite score (S_final).

WHY THE OLD FORMULA WAS REPLACED
--------------------------------
    S_final = 0.35*Demand + 0.25*(100 - Competition) + 0.20*Timing + 0.20*Moat

Four measured problems with that weighted arithmetic mean:

1. IT WAS COMPENSATORY, SO FATAL FLAWS AVERAGED AWAY.
   An idea nobody wants (demand 5) with great timing, a strong moat and an
   open field scored 60 — "Promising, validate more". Zero demand means zero
   business regardless of what else is true. A business needs demand AND a way
   to win; those aren't interchangeable, so they shouldn't be summed.

2. RANGE COMPRESSION. Averaging four independent 0-100 judgments roughly
   halves the spread (stdev 14.9 against 28.9 for a single component). 98% of
   outcomes landed between 17 and 83, and each verdict extreme was reachable
   in only ~10% of the input space. The score barely moved when inputs did.

3. FAILED SUB-CALLS COUNTED AS REAL SIGNAL. A defaulted 50 contributed its
   full weight as though measured. Four failures produced exactly 50 —
   indistinguishable from a genuine middling read.

4. COMPETITION WAS SCORED IN ISOLATION. An empty field always contributed the
   full 25 points whether that meant untapped demand or no demand at all, and
   defensibility was scored without reference to how contested the space is.

THE NEW MODEL
-------------
Three factors, combined as a weighted geometric mean, then sharpened by how
much was actually measured:

  Demand          the market exists and wants this              weight 0.45
  Contestability  can you actually take a share of it            weight 0.35
  Timing          is now the moment                              weight 0.20

Contestability folds competition and moat into one axis, because defensibility
only means something relative to how contested the space is:

    contestability = 100 * moat / (moat + competition)

A contest ratio. An open field scores high regardless of moat (nobody to fight
yet); a saturated field scores high only with a real moat; equal pressure and
equal defence gives 50.

The geometric mean is non-compensatory — any factor near zero drags the
composite down, which is the behaviour a viability score should have.

A gain curve then expands the range around the midpoint:

    p_out = p^k / (p^k + (1-p)^k)

with k scaled by confidence, i.e. the share of total weight contributed by
sub-calls that actually returned:

    k_effective = 1 + (SHARPNESS - 1) * confidence

Full data gets full sharpening. Partial data gets a flatter curve, and any
unmeasured factor is held at a neutral 50, so a missing component pulls the
score toward the middle rather than letting whatever survived speak louder.
That is the fix for an earlier version of this module in which a single
surviving sub-call produced a MORE extreme score than all four together.

CALIBRATION NOTE
----------------
Across uniform random inputs the new score has mean ~42 (against 50) and
stdev ~25 (against 15). The downward shift is deliberate: a product of factors
is pessimistic by construction, which suits a model where several things all
have to be true at once. Verdict thresholds are retuned to match. Only the
midpoint is a fixed point of the curve — 50 in gives 50 out, but "all inputs
at X" does NOT return X once sharpening is applied.
"""

from math import exp, log
from typing import Optional, Set

from llm_json import clamp_score

# Must sum to 1.0. Tune against real outcomes if you ever back-test.
SCORE_WEIGHTS = {
    "demand": 0.45,
    "contestability": 0.35,
    "timing": 0.20,
}

# Range expansion at full confidence. 1.0 = off (pure geometric mean).
SHARPNESS = 1.1

# Nothing may be exactly 0, or a single judgment annihilates the product.
# A floor of 1 still caps hard — demand 0 can't score above ~13 — without
# making the result unrecoverably zero.
_FLOOR = 1.0

_NEUTRAL = 50.0

# Which raw sub-calls feed which factor.
_FACTOR_SOURCES = {
    "demand": {"demandScore"},
    "contestability": {"competitionIntensity", "moatScore"},
    "timing": {"timingScore"},
}


def compute_contestability(competition_intensity, moat) -> float:
    """
    Folds competition and moat into one 0-100 axis via a contest ratio.

      competition   0, moat   0  ->  50.0   (open field, nothing defensible yet)
      competition   0, moat 100  ->  99.0   (open field and hard to copy)
      competition 100, moat   0  ->   1.0   (saturated and trivially copied)
      competition 100, moat 100  ->  50.0   (saturated but genuinely defensible)
      competition  75, moat  25  ->  25.0
      competition  25, moat  75  ->  75.0
    """
    c = max(_FLOOR, float(competition_intensity))
    m = max(_FLOOR, float(moat))
    return 100.0 * m / (m + c)


def _sharpen(pct: float, k: float) -> float:
    """Expands the range around 50. k=1 is the identity; 50 is a fixed point."""
    p = min(1.0, max(0.0, pct / 100.0))
    if k <= 1.0:
        return 100.0 * p
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 100.0
    num = p ** k
    return 100.0 * num / (num + (1.0 - p) ** k)


def _factors_and_confidence(components: dict, measured: Optional[Set[str]]):
    """
    Returns (factors, active set, confidence, coerced inputs). Unmeasured
    factors are held at a neutral 50 and excluded from the confidence weight.
    """
    demand = clamp_score(components.get("demandScore"))
    competition = clamp_score(components.get("competitionIntensity"))
    timing = clamp_score(components.get("timingScore"))
    moat = clamp_score(components.get("moatScore"))

    raw = {
        "demand": float(demand),
        "contestability": compute_contestability(competition, moat),
        "timing": float(timing),
    }

    if measured is None:
        active = set(raw)
    else:
        active = {
            name for name, sources in _FACTOR_SOURCES.items()
            if sources & measured
        }

    factors = {
        name: (value if name in active else _NEUTRAL)
        for name, value in raw.items()
    }

    # Confidence is weight-share backed by real data. Contestability draws on
    # two sub-calls, so losing one costs half its weight rather than none:
    # a defaulted moat of 50 is still a placeholder, even though competition
    # came back and the axis is computable.
    confidence = 0.0
    for name, weight in SCORE_WEIGHTS.items():
        if name not in active:
            continue
        sources = _FACTOR_SOURCES[name]
        share = 1.0 if measured is None else len(sources & measured) / len(sources)
        confidence += weight * share

    inputs = {
        "demandScore": demand,
        "competitionIntensity": competition,
        "timingScore": timing,
        "moatScore": moat,
    }
    return factors, active, confidence, inputs


def compute_final_score(components: dict, measured: Optional[Set[str]] = None) -> int:
    """
    Weighted geometric mean of demand / contestability / timing, sharpened in
    proportion to how much of the score was actually measured.

    `components` takes the raw sub-scores: demandScore, competitionIntensity,
    timingScore, moatScore. Values are coerced and clamped, so this never
    raises on a string, a None, or an out-of-range number.

    `measured` is the set of sub-call keys that succeeded. Pass None to treat
    everything as measured.
    """
    factors, _active, confidence, _inputs = _factors_and_confidence(components, measured)

    log_sum = 0.0
    for name, weight in SCORE_WEIGHTS.items():
        log_sum += weight * log(max(_FLOOR, factors[name]) / 100.0)

    raw_pct = 100.0 * exp(log_sum)
    k = 1.0 + (SHARPNESS - 1.0) * confidence
    return int(round(_sharpen(raw_pct, k)))


def score_breakdown(components: dict, measured: Optional[Set[str]] = None) -> dict:
    """
    The same maths with each factor exposed, so the UI can show how the score
    was built. Contributions are MULTIPLICATIVE — each factor's multiplier is
    (value/100) ** weight, and their product is the pre-sharpening composite.
    """
    factors, active, confidence, inputs = _factors_and_confidence(components, measured)

    log_sum = 0.0
    rows = []
    for name in ("demand", "contestability", "timing"):
        weight = SCORE_WEIGHTS[name]
        value = max(_FLOOR, factors[name])
        log_sum += weight * log(value / 100.0)
        rows.append({
            "factor": name,
            "value": round(factors[name], 1),
            "weight": weight,
            "measured": name in active,
            "multiplier": round((value / 100.0) ** weight, 4),
        })

    raw_pct = 100.0 * exp(log_sum)
    k = 1.0 + (SHARPNESS - 1.0) * confidence

    return {
        "score": int(round(_sharpen(raw_pct, k))),
        "compositeBeforeSharpening": round(raw_pct, 1),
        "factors": rows,
        "inputs": inputs,
        "contestability": round(factors["contestability"], 1),
        "confidence": round(confidence, 3),
        "sharpness": round(k, 3),
        "model": "weighted geometric mean of demand x contestability x timing, confidence-sharpened",
    }


def score_to_verdict(score: int) -> str:
    """
    Thresholds retuned for the new distribution. The geometric mean plus
    sharpening pushes weak ideas genuinely low, so the bands sit lower than
    the old 70/50/30 split.
    """
    if score >= 68:
        return "Worth a pilot"
    if score >= 45:
        return "Promising, validate more"
    if score >= 25:
        return "Wait and validate more"
    return "Hard pass for now"