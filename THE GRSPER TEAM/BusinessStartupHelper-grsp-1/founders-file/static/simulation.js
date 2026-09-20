/*
 * Business > Visualization — simulation engine (v5).
 *
 * v5 fixes six places where the model diverged from how small-business
 * pro-forma models (and the actual tax code) behave. Each was verified
 * against v4 numerically.
 *
 *  1. TAX WAS COMPUTED ON CASH FLOW, NOT TAXABLE INCOME.
 *     v4: preTaxNet = operatingCashFlow - loanPayment, and tax was charged on
 *     that. But loan PRINCIPAL isn't deductible — only interest is. On a
 *     $60k loan at 7.5% this understated taxable income by ~$835/month, about
 *     $209/month of tax. v5 separates the two: cash flow still subtracts the
 *     whole payment, taxable income subtracts only the interest.
 *
 *  2. LOSSES WERE THROWN AWAY INSTEAD OF CARRIED FORWARD.
 *     v4: taxOwed = max(0, quarterAccrual) * rate. A business losing $8,960 in
 *     Q1 and making $20,225 in Q2 paid tax on the full Q2 profit — $5,056 on a
 *     six-month net of $11,265. A real filer nets the loss against the profit.
 *     v5 carries losses forward and offsets future quarters.
 *
 *  3. STARTUP COSTS HAD NO TAX TREATMENT AT ALL.
 *     v4 spent the cash and stopped there. v5 applies a simplified IRC §195:
 *     up to $5,000 deducted in month 1, the remainder amortized straight-line
 *     over 180 months.
 *
 *  4. FIXED COSTS UN-HIRED THEMSELVES.
 *     v4 recomputed stepsCrossed from CURRENT revenue every month with no
 *     memory, so a revenue dip instantly reversed a step-up. Staff and leases
 *     don't work that way. v5 ratchets: steps never decrease within a run.
 *
 *  5. THE FOUNDER WENT BROKE WHILE THE BUSINESS THRIVED.
 *     v4 paid personal expenses out of a personal balance that received
 *     nothing unless a stream was flagged non-reinvest. A 12-month run ended
 *     with $66,325 in the business and -$36,000 personal. No sole proprietor
 *     watches their own rent go unpaid with that sitting in the business
 *     account. v5 draws from the business to cover personal expenses when it
 *     can afford to, and reports `personalShortfall` when it can't — which is
 *     the genuinely useful signal.
 *
 *  6. REVENUE WAS BILLED AND COLLECTED IN THE SAME INSTANT.
 *     No DSO. A B2B service invoicing net-30 has materially worse early
 *     runway than v4 showed, and runway is the number this tool exists to
 *     produce. v5 adds `collectionLagMonths`: work is performed and costed
 *     when billed, cash arrives later. Defaults to 0 (cash-at-point-of-sale).
 *
 *  Also: operating break-even and payback are now reported separately (they
 *  are distinct milestones in any pro-forma), runway uses a 3-month average
 *  burn instead of a single month's delta, and a missed loan payment accrues
 *  at a default rate rather than the note rate.
 *
 * SIMPLIFICATIONS STILL IN PLACE (deliberate, not bugs):
 *  - One blended effective tax rate, not separate SE tax (15.3% on 92.35% of
 *    net earnings) plus graduated income tax, and no half-SE-tax deduction.
 *  - Estimated tax is paid one month after each quarter closes, rather than on
 *    the actual 1040-ES deadlines (Apr 15 / Jun 15 / Sep 15 / Jan 15).
 *  - No depreciation schedule for equipment; no seasonality; no explicit
 *    churn (it's folded into each stream's net growth rate); negative business
 *    cash carries no overdraft interest.
 */

/** Coerces anything the UI or an LLM might hand us into a usable number. */
function num(value, fallback = 0) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : fallback;
  if (typeof value === 'string') {
    const cleaned = parseFloat(value.replace(/[$,\s%]/g, ''));
    return Number.isFinite(cleaned) ? cleaned : fallback;
  }
  return fallback;
}

// Simplified IRC §195 startup-cost treatment.
const STARTUP_IMMEDIATE_DEDUCTION = 5000;
const STARTUP_AMORTIZATION_MONTHS = 180;

// A missed installment payment doesn't accrue at the note rate in practice.
const DEFAULT_RATE_PREMIUM_PCT = 5.0;

function computeStreamRevenue(revenueStreams, monthNumber) {
  let totalUncapped = 0;
  let reinvestUncapped = 0;

  (revenueStreams || []).forEach((stream) => {
    // A stream cannot decline by more than 100%/month. Below -100% the
    // (1+g)^n term flips sign each month and revenue oscillates between
    // positive and negative, which is not a business, it's an artifact.
    const rate = Math.max(-0.99, num(stream.growthRatePct) / 100);
    const factor = Math.pow(1 + rate, monthNumber - 1);
    const streamRevenue = num(stream.monthlyAmount) * factor;
    totalUncapped += streamRevenue;
    if (stream.reinvest !== false) reinvestUncapped += streamRevenue;
  });

  return { totalUncapped, reinvestUncapped };
}

/**
 * Saturates only the portion above `floor` (month-1 uncapped revenue), so
 * month 1 returns exactly what was entered while growth beyond it bends
 * asymptotically toward the ceiling.
 */
function applyRevenueSaturation(totalUncapped, revenueCeiling, floor = 0) {
  const ceiling = num(revenueCeiling, Infinity);
  if (!ceiling || ceiling === Infinity || ceiling <= 0) return totalUncapped;
  if (ceiling <= floor) return totalUncapped;
  if (totalUncapped <= floor) return totalUncapped;

  const headroom = ceiling - floor;
  return floor + headroom * (1 - Math.exp(-(totalUncapped - floor) / headroom));
}

/** Billed revenue for a given month — deterministic, so collection lag needs no history. */
function billedRevenueForMonth(inputs, monthNumber, saturationFloor) {
  if (monthNumber < 1) return 0;
  const { totalUncapped } = computeStreamRevenue(inputs.revenueStreams, monthNumber);
  return applyRevenueSaturation(totalUncapped, inputs.revenueCeiling, saturationFloor);
}

/**
 * Deduction allowed this month for one-time startup costs.
 * §195: up to $5,000 immediately, remainder over 180 months.
 */
function startupDeductionForMonth(oneTimeStartupCost, monthNumber) {
  const cost = num(oneTimeStartupCost);
  if (cost <= 0) return 0;

  const immediate = Math.min(STARTUP_IMMEDIATE_DEDUCTION, cost);
  const amortizable = Math.max(0, cost - immediate);
  const monthly = amortizable / STARTUP_AMORTIZATION_MONTHS;

  let deduction = monthNumber <= STARTUP_AMORTIZATION_MONTHS ? monthly : 0;
  if (monthNumber === 1) deduction += immediate;
  return deduction;
}

/**
 * Computes a single month.
 *
 * @param {Object} inputs
 * @param {number} monthNumber
 * @param {number} prevBusinessCash
 * @param {number} prevPersonalCash
 * @param {number} prevLoanBalance
 * @param {number} baselineRevenue  billed revenue at month 1
 * @param {Object} taxState  mutated in place: {quarterTaxable, pendingTaxPayment, lossCarryforward}
 * @param {Object} [opts]  {saturationFloor, settleOutstandingTax, prevStepsCrossed}
 */
function computeMonth(
  inputs, monthNumber, prevBusinessCash, prevPersonalCash, prevLoanBalance,
  baselineRevenue, taxState, opts = {}
) {
  const saturationFloor = num(opts.saturationFloor, 0);
  const settleOutstandingTax = opts.settleOutstandingTax === true;
  const prevStepsCrossed = num(opts.prevStepsCrossed, 0);

  const fixedExpensesMonthly = num(inputs.fixedExpensesMonthly);
  const fixedExpenseStepThresholdPct = num(inputs.fixedExpenseStepThresholdPct);
  const fixedExpenseStepAmount = num(inputs.fixedExpenseStepAmount);
  const variableExpensesMonthly = num(inputs.variableExpensesMonthly);
  const monthlyLoanPayment = num(inputs.monthlyLoanPayment);
  const loanInterestRatePct = num(inputs.loanInterestRatePct);
  const monthlyPersonalExpenses = num(inputs.monthlyPersonalExpenses);
  const taxRate = num(inputs.estimatedTaxRatePct) / 100;
  const collectionLagMonths = Math.max(0, Math.round(num(inputs.collectionLagMonths)));

  // ---- Revenue: billed now, collected later ----
  const { totalUncapped, reinvestUncapped } = computeStreamRevenue(inputs.revenueStreams, monthNumber);
  const revenueBilled = applyRevenueSaturation(totalUncapped, inputs.revenueCeiling, saturationFloor);
  const revenueCollected = collectionLagMonths === 0
    ? revenueBilled
    : billedRevenueForMonth(inputs, monthNumber - collectionLagMonths, saturationFloor);

  const reinvestShare = totalUncapped > 0 ? reinvestUncapped / totalUncapped : 1;

  // ---- Costs scale with work PERFORMED (billed), not cash received ----
  const volumeFactor = baselineRevenue > 0 ? revenueBilled / baselineRevenue : 1;
  const variablePortion = variableExpensesMonthly * volumeFactor;

  // Fixed costs RATCHET — a revenue dip doesn't un-hire staff or end a lease.
  let stepsCrossed = prevStepsCrossed;
  if (fixedExpenseStepThresholdPct > 0 && baselineRevenue > 0) {
    const growthPct = ((revenueBilled - baselineRevenue) / baselineRevenue) * 100;
    const stepsNow = Math.max(0, Math.floor(growthPct / fixedExpenseStepThresholdPct));
    stepsCrossed = Math.max(prevStepsCrossed, stepsNow);
  }
  const fixedPortion = fixedExpensesMonthly + stepsCrossed * fixedExpenseStepAmount;

  const expenses = fixedPortion + variablePortion;
  const operatingCashFlow = revenueCollected - expenses;

  // ---- Loan ----
  let loanPayment = 0;
  let interestPortion = 0;
  let principalPortion = 0;
  let newLoanBalance = prevLoanBalance;
  let loanDefaulted = false;

  if (prevLoanBalance > 0 && monthlyLoanPayment > 0) {
    const monthlyRate = (loanInterestRatePct / 100) / 12;
    interestPortion = prevLoanBalance * monthlyRate;
    const amountOwed = prevLoanBalance + interestPortion;
    const scheduledPayment = Math.min(monthlyLoanPayment, amountOwed);

    const availableCash = prevBusinessCash + operatingCashFlow - taxState.pendingTaxPayment;
    if (availableCash >= scheduledPayment) {
      loanPayment = scheduledPayment;
      principalPortion = loanPayment - interestPortion;
      newLoanBalance = Math.max(0, prevLoanBalance - principalPortion);
    } else {
      // Missed payment: unpaid interest capitalizes, and at a default rate —
      // a missed installment doesn't keep accruing at the note rate.
      const defaultRate = ((loanInterestRatePct + DEFAULT_RATE_PREMIUM_PCT) / 100) / 12;
      loanDefaulted = true;
      loanPayment = 0;
      principalPortion = 0;
      interestPortion = prevLoanBalance * defaultRate;
      newLoanBalance = prevLoanBalance + interestPortion;
    }
  }

  // ---- Taxable income: NOT the same as cash flow ----
  // Only loan INTEREST is deductible; principal repayment is a balance-sheet
  // movement. Startup costs get §195 treatment.
  const startupDeduction = startupDeductionForMonth(inputs.oneTimeStartupCost, monthNumber);
  const taxableIncome = revenueCollected - fixedPortion - variablePortion
    - interestPortion - startupDeduction;

  taxState.quarterTaxable += taxableIncome;

  let taxPaidThisMonth = taxState.pendingTaxPayment;
  taxState.pendingTaxPayment = 0;

  let taxAccruedThisQuarter = 0;
  const closeQuarter = (monthNumber % 3 === 0);
  if (closeQuarter || settleOutstandingTax) {
    let q = taxState.quarterTaxable;
    if (q < 0) {
      // Loss carried forward to offset future quarters, rather than discarded.
      taxState.lossCarryforward += -q;
      q = 0;
    } else {
      const offset = Math.min(taxState.lossCarryforward, q);
      taxState.lossCarryforward -= offset;
      q -= offset;
    }
    taxAccruedThisQuarter = q * taxRate;
    taxState.quarterTaxable = 0;

    if (settleOutstandingTax) {
      // Final month: settle rather than leaving a quarter dangling forever.
      taxPaidThisMonth += taxAccruedThisQuarter;
    } else {
      taxState.pendingTaxPayment = taxAccruedThisQuarter;
    }
  }

  const netCashFlow = operatingCashFlow - loanPayment - taxPaidThisMonth;

  // ---- Profit split, then the owner draw a real sole proprietor takes ----
  // Losses are the business's alone; only profit is shared.
  let businessRetainedCashFlow;
  let discretionaryDraw;
  if (netCashFlow >= 0) {
    businessRetainedCashFlow = netCashFlow * reinvestShare;
    discretionaryDraw = netCashFlow * (1 - reinvestShare);
  } else {
    businessRetainedCashFlow = netCashFlow;
    discretionaryDraw = 0;
  }

  // The founder still has to eat. Draw whatever the discretionary split
  // doesn't already cover, to the extent the business can actually pay it.
  const cashAfterRetention = prevBusinessCash + businessRetainedCashFlow;
  const personalAvailable = prevPersonalCash + discretionaryDraw;
  const neededDraw = Math.max(0, monthlyPersonalExpenses - personalAvailable);
  const necessityDraw = Math.min(neededDraw, Math.max(0, cashAfterRetention));

  const ownerDraw = discretionaryDraw + necessityDraw;
  const businessCashBalance = cashAfterRetention - necessityDraw;

  // You cannot spend money you do not have. The personal balance floors at
  // zero and the gap is reported as THIS MONTH's shortfall — letting it go
  // negative made the next month's "needed draw" try to fund the entire
  // backlog, so the shortfall compounded instead of recurring.
  const personalBeforeSpend = prevPersonalCash + ownerDraw;
  const personalSpend = Math.min(monthlyPersonalExpenses, Math.max(0, personalBeforeSpend));
  const personalShortfall = monthlyPersonalExpenses - personalSpend;
  const personalCashBalance = personalBeforeSpend - personalSpend;

  return {
    month: monthNumber,
    revenue: revenueBilled,        // kept as `revenue` for existing callers
    revenueBilled,
    revenueCollected,
    revenueUncapped: totalUncapped,
    fixedPortion,
    variablePortion,
    expenses,
    stepsCrossed,
    operatingCashFlow,
    loanPayment,
    loanInterestPortion: interestPortion,
    loanPrincipalPortion: principalPortion,
    loanBalance: newLoanBalance,
    loanDefaulted,
    startupDeduction,
    taxableIncome,
    lossCarryforward: taxState.lossCarryforward,
    taxAccruedThisQuarter,
    taxPaid: taxPaidThisMonth,
    preTaxNet: operatingCashFlow - loanPayment, // cash figure, kept for compat
    netCashFlow,
    reinvestShare,
    businessRetainedCashFlow,
    ownerDraw,
    personalWithdrawal: ownerDraw, // alias for existing callers
    personalShortfall,
    businessCashBalance,
    personalCashBalance,
    cashBalance: businessCashBalance, // alias for existing callers
  };
}

function runSimulation(inputs) {
  const personalInvestment = num(inputs.personalInvestment);
  const loanAmount = num(inputs.loanAmount);
  const oneTimeStartupCost = num(inputs.oneTimeStartupCost);
  const monthsToForecast = Math.max(1, Math.round(num(inputs.monthsToForecast, 12) || 12));
  const stopLossThreshold = num(inputs.stopLossThreshold);

  const openingCash = personalInvestment + loanAmount - oneTimeStartupCost;
  const stakeAtRisk = personalInvestment;

  const { totalUncapped: month1Uncapped } = computeStreamRevenue(inputs.revenueStreams, 1);
  const saturationFloor = month1Uncapped;
  const baselineRevenue = applyRevenueSaturation(month1Uncapped, inputs.revenueCeiling, saturationFloor);

  const months = [];
  let businessCashBalance = openingCash;
  let personalCashBalance = 0;
  let loanBalance = loanAmount;
  let stepsCrossed = 0;
  let operatingBreakEvenMonth = null;
  let paybackMonth = null;
  let haltedAtMonth = null;
  let firstPersonalShortfallMonth = null;
  let cumulativeOwnerDraw = 0;
  let cumulativePersonalShortfall = 0;
  const taxState = { quarterTaxable: 0, pendingTaxPayment: 0, lossCarryforward: 0 };

  for (let m = 1; m <= monthsToForecast; m++) {
    const monthData = computeMonth(
      inputs, m, businessCashBalance, personalCashBalance, loanBalance,
      baselineRevenue, taxState,
      {
        saturationFloor,
        settleOutstandingTax: m === monthsToForecast,
        prevStepsCrossed: stepsCrossed,
      }
    );
    businessCashBalance = monthData.businessCashBalance;
    personalCashBalance = monthData.personalCashBalance;
    loanBalance = monthData.loanBalance;
    stepsCrossed = monthData.stepsCrossed;
    cumulativeOwnerDraw += monthData.ownerDraw;
    cumulativePersonalShortfall += monthData.personalShortfall;
    months.push(monthData);

    // Two distinct milestones, reported separately.
    if (operatingBreakEvenMonth === null && monthData.operatingCashFlow >= 0) {
      operatingBreakEvenMonth = m;
    }
    // Payback = value the business has generated, whether it stayed in the
    // business or was drawn out. Measuring retained cash alone understated
    // recovery the moment the owner started drawing a living from it.
    const valueReturned = (businessCashBalance - openingCash) + cumulativeOwnerDraw;
    if (paybackMonth === null && valueReturned >= stakeAtRisk) {
      paybackMonth = m;
    }
    if (firstPersonalShortfallMonth === null && monthData.personalShortfall > 0) {
      firstPersonalShortfallMonth = m;
    }

    if (haltedAtMonth === null && businessCashBalance <= stopLossThreshold) {
      haltedAtMonth = m;
      break;
    }
  }

  return {
    months,
    operatingBreakEvenMonth,
    paybackMonth,
    breakEvenMonth: paybackMonth, // alias for existing callers
    haltedAtMonth,
    firstPersonalShortfallMonth,
    // The final month settles the outstanding tax quarter, which is a one-off
    // outflow. Including it in the burn window made a business whose cash was
    // climbing report a finite runway. Exclude it when there's enough history.
    runwayMonths: computeRunway(
      (haltedAtMonth === null && months.length >= 4) ? months.slice(0, -1) : months,
      stopLossThreshold,
      haltedAtMonth
    ),
    baselineRevenue,
    saturationFloor,
    openingCash,
    stakeAtRisk,
    stepsCrossed,
    cumulativeOwnerDraw,
    cumulativePersonalShortfall,
    taxState,
  };
}

/**
 * Months of cash left at the recent burn rate, averaged over up to three
 * months rather than a single month's delta — one lumpy month (a quarterly
 * tax payment) shouldn't set the runway figure.
 */
function computeRunway(months, stopLossThreshold, haltedAtMonth) {
  if (haltedAtMonth !== null) return 0;
  if (months.length < 2) return null;

  // Average over a window rather than a single month's delta, so one lumpy
  // quarterly tax payment doesn't set the runway figure on its own.
  const window = months.slice(-Math.min(4, months.length));
  const span = window.length - 1;
  const burn = (window[0].businessCashBalance - window[window.length - 1].businessCashBalance) / span;
  if (burn <= 0) return null;

  const last = months[months.length - 1];
  const remaining = (last.businessCashBalance - stopLossThreshold) / burn;
  if (remaining <= 0) return 0;
  // A very permissive stop-loss produces an arithmetically correct but
  // meaningless figure (hundreds of thousands of months). Past the 50-year
  // projection horizon, report "no ceiling in sight" instead of a number.
  return remaining > 600 ? null : Math.ceil(remaining);
}

/**
 * Clone simulation state for a caller that wants to extend the run (see
 * GraphViz's fast-forward). A shallow spread is not enough once taxState
 * holds nested values.
 */
function cloneSimState(state) {
  return {
    quarterTaxable: num(state && state.quarterTaxable),
    pendingTaxPayment: num(state && state.pendingTaxPayment),
    lossCarryforward: num(state && state.lossCarryforward),
  };
}

window.runSimulation = runSimulation;
window.computeMonth = computeMonth;
window.computeStreamRevenue = computeStreamRevenue;
window.cloneSimState = cloneSimState;
window.simNum = num;