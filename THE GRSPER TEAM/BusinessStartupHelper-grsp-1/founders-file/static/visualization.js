/*
 * Business > Visualization — input panel logic.
 * Kept separate from app.js so this feature's state/behavior doesn't mix with
 * the Sentiment/Model/Tax logic already living there.
 *
 * Hard dependency: this tab requires Sentiment Score to have run first.
 * app.js gates on state.round1 and shows #vizGate instead of mounting.
 *
 * v5 changes:
 *  - Two new inputs the simulation now actually consumes: one-time startup
 *    cost (spent at month 0, so break-even finally means something) and
 *    collection lag (work is costed when performed, cash arrives later).
 *  - Fourth origin colour, "adjusted", for values the backend's plausibility
 *    review had to correct. Previously a hardcoded fallback could appear in
 *    the yellow "LLM estimate" colour, which misrepresented where it came
 *    from. The origin map returned by /estimate is now used instead of every
 *    field being tagged 'llm' unconditionally.
 *  - The plausibility report is rendered, so corrections and warnings are
 *    visible rather than silent.
 *  - gatherSimulationInputs coerces through the same helper the engine uses.
 *    `parseFloat(x) || 0` turned a blank field AND "$5,000" into 0 alike.
 *  - Operating break-even and payback are reported separately, and a personal
 *    shortfall is surfaced.
 */

const VizState = {
  idea: '',
  sentiment: null,
  origin: {}, // origin[fieldId] = 'llm' | 'user' | 'computed' | 'adjusted'
  streams: [], // [{ id, name, monthlyAmount, growthRatePct, reinvest, origin }]
  loan: {
    amount: null,
    type: null,
    interestRatePct: null,
    termMonths: null,
    monthlyPayment: null,
    isFallbackEstimate: false, // true if loan terms came from the local fallback
    pendingFetch: null,        // in-flight fetchLoanTerms() promise, or null
  },
  plausibility: null,
  simulation: null,
};

const $v = (id) => document.getElementById(id);
let streamIdCounter = 0;

async function vizPostJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}

/* ---- Origin colours: LLM estimate / your input / calculated / adjusted ---- */
const ORIGIN_CLASSES = ['origin-llm', 'origin-user', 'origin-computed', 'origin-adjusted'];

function originClass(origin) {
  if (origin === 'llm') return 'origin-llm';
  if (origin === 'user') return 'origin-user';
  if (origin === 'adjusted') return 'origin-adjusted';
  return 'origin-computed';
}

function renderOriginKey() {
  return `
    <div class="viz-key">
      <span class="viz-key-item"><span class="viz-dot origin-llm"></span>LLM estimate</span>
      <span class="viz-key-item"><span class="viz-dot origin-user"></span>Your input</span>
      <span class="viz-key-item"><span class="viz-dot origin-computed"></span>Fallback / calculated</span>
      <span class="viz-key-item"><span class="viz-dot origin-adjusted"></span>Adjusted for plausibility</span>
    </div>
  `;
}

function setFieldOrigin(fieldId, origin) {
  VizState.origin[fieldId] = origin;
  const el = $v(fieldId);
  if (!el) return;
  ORIGIN_CLASSES.forEach((c) => el.classList.remove(c));
  el.classList.add(originClass(origin));
}

function bindOverrideBehavior(fieldId) {
  const el = $v(fieldId);
  if (!el) return;
  el.addEventListener('input', () => setFieldOrigin(fieldId, 'user'));
}

/* ==================== REVENUE STREAMS ==================== */

// Same coercion the engine uses, so the UI and the maths agree on what
// "4,000" or "$4000" or "" means.
function streamNum(value, fallback = 0) {
  if (window.simNum) return window.simNum(value, fallback);
  if (typeof value === 'number') return Number.isFinite(value) ? value : fallback;
  if (typeof value === 'string') {
    const n = parseFloat(value.replace(/[$,\s%]/g, ''));
    return Number.isFinite(n) ? n : fallback;
  }
  return fallback;
}

// Sanity bounds. Outside these the number is almost certainly a typo, and
// compounding it for 60 months produces absurd output. The engine clamps
// growth at -99% too, since below -100% the (1+g)^n term flips sign each
// month and revenue oscillates between positive and negative.
const STREAM_LIMITS = {
  maxMonthlyAmount: 10000000, // $10M/mo
  maxGrowthPct: 100,          // 100%/mo doubles every month
  minGrowthPct: -99,
};

function validateStream(stream) {
  const problems = [];
  if (stream.monthlyAmount < 0) problems.push('Monthly amount can\'t be negative.');
  if (stream.monthlyAmount > STREAM_LIMITS.maxMonthlyAmount) problems.push('Monthly amount looks like a typo.');
  if (stream.growthRatePct > STREAM_LIMITS.maxGrowthPct) problems.push('Growth above 100%/mo compounds to nonsense.');
  if (stream.growthRatePct < STREAM_LIMITS.minGrowthPct) problems.push('A decline steeper than -99%/mo isn\'t possible; the engine will clamp it.');
  return problems;
}

function addStreamRow(data = {}) {
  const id = `stream-${streamIdCounter++}`;
  const stream = {
    id,
    name: typeof data.name === 'string' && data.name.trim() ? data.name.trim() : 'New stream',
    // streamNum, not `|| 0` — a legitimate 0 survives and a string is parsed.
    monthlyAmount: streamNum(data.monthlyAmount, 0),
    growthRatePct: streamNum(data.growthRatePct, 0),
    reinvest: data.reinvest !== false,
    origin: data.origin || 'user',
  };
  VizState.streams.push(stream);

  // Append only the new row. Existing rows keep their DOM, so focus and caret
  // position in a row you're mid-way through editing survive the add.
  const container = $v('vizStreamsContainer');
  if (container) container.appendChild(buildStreamRow(stream));

  refreshStreamSummary();
  recomputeSimulation();
  return stream;
}

function removeStreamRow(id) {
  VizState.streams = VizState.streams.filter((s) => s.id !== id);
  const container = $v('vizStreamsContainer');
  const row = container && container.querySelector(`[data-stream-id="${id}"]`);
  if (row) row.remove();

  refreshStreamSummary();
  recomputeSimulation();
}

/** Builds one row as real DOM (no innerHTML), with listeners attached. */
function buildStreamRow(stream) {
  const cls = originClass(stream.origin);

  const row = document.createElement('div');
  row.className = 'viz-stream-row';
  row.dataset.streamId = stream.id;

  const nameEl = document.createElement('input');
  nameEl.type = 'text';
  nameEl.className = `viz-stream-name ${cls}`;
  nameEl.placeholder = 'Stream name';
  nameEl.value = stream.name;   // .value assignment — no escaping needed

  const amountEl = document.createElement('input');
  amountEl.type = 'number';
  amountEl.className = `viz-stream-amount ${cls}`;
  amountEl.placeholder = '$/month';
  amountEl.min = '0';
  amountEl.value = stream.monthlyAmount;

  const growthEl = document.createElement('input');
  growthEl.type = 'number';
  growthEl.className = `viz-stream-growth ${cls}`;
  growthEl.placeholder = 'Growth %';
  growthEl.step = '0.1';
  growthEl.value = stream.growthRatePct;

  const reinvestLabel = document.createElement('label');
  reinvestLabel.className = 'viz-stream-reinvest';
  const reinvestEl = document.createElement('input');
  reinvestEl.type = 'checkbox';
  reinvestEl.className = 'viz-stream-reinvest-cb';
  reinvestEl.checked = stream.reinvest;
  reinvestLabel.appendChild(reinvestEl);
  // The label text was missing, leaving a bare unlabelled checkbox.
  reinvestLabel.appendChild(document.createTextNode(' Reinvest'));

  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'ghost viz-stream-remove';
  removeBtn.title = 'Remove stream';
  removeBtn.textContent = '×';

  row.append(nameEl, amountEl, growthEl, reinvestLabel, removeBtn);

  // Inline per-row validation note, spanning the full grid width.
  const noteEl = document.createElement('div');
  noteEl.className = 'viz-stream-note';
  noteEl.style.display = 'none';
  row.appendChild(noteEl);

  const markUser = () => {
    stream.origin = 'user';
    [nameEl, amountEl, growthEl].forEach((el) => {
      ORIGIN_CLASSES.forEach((c) => el.classList.remove(c));
      el.classList.add('origin-user');
    });
  };

  const showProblems = () => {
    const problems = validateStream(stream);
    if (problems.length) {
      noteEl.textContent = problems.join(' ');
      noteEl.style.display = 'block';
      amountEl.classList.add('viz-stream-invalid');
      growthEl.classList.add('viz-stream-invalid');
    } else {
      noteEl.style.display = 'none';
      amountEl.classList.remove('viz-stream-invalid');
      growthEl.classList.remove('viz-stream-invalid');
    }
    return problems.length === 0;
  };

  nameEl.addEventListener('input', () => {
    stream.name = nameEl.value;
    markUser();
    refreshStreamSummary();
  });

  amountEl.addEventListener('input', () => {
    stream.monthlyAmount = streamNum(amountEl.value, 0);
    markUser();
    showProblems();
    refreshStreamSummary();
    recomputeSimulation();
  });

  growthEl.addEventListener('input', () => {
    stream.growthRatePct = streamNum(growthEl.value, 0);
    markUser();
    showProblems();
    refreshStreamSummary();
    recomputeSimulation();
  });

  reinvestEl.addEventListener('change', () => {
    stream.reinvest = reinvestEl.checked;
    markUser();   // toggling IS your edit — it was silently staying "llm"
    refreshStreamSummary();
    recomputeSimulation();
  });

  removeBtn.addEventListener('click', () => removeStreamRow(stream.id));

  showProblems();
  return row;
}

/** Full rebuild — only used when seeding the panel from scratch. */
function renderStreamRows() {
  const container = $v('vizStreamsContainer');
  if (!container) return;
  container.textContent = '';
  VizState.streams.forEach((stream) => container.appendChild(buildStreamRow(stream)));
  refreshStreamSummary();
}

/**
 * Month-1 combined revenue plus the reinvest split, so "why is my graph flat"
 * and "why is the business starving" are answerable at a glance.
 */
function refreshStreamSummary() {
  const el = $v('vizStreamTotals');
  const warnEl = $v('vizStreamWarning');
  if (!el) return;

  const streams = VizState.streams;
  const total = streams.reduce((sum, s) => sum + streamNum(s.monthlyAmount, 0), 0);
  const reinvested = streams
    .filter((s) => s.reinvest !== false)
    .reduce((sum, s) => sum + streamNum(s.monthlyAmount, 0), 0);
  const sharePct = total > 0 ? Math.round((reinvested / total) * 100) : 100;

  el.textContent = streams.length
    ? `${streams.length} stream${streams.length === 1 ? '' : 's'} — ` +
      `$${Math.round(total).toLocaleString()}/month at month 1, ` +
      `${sharePct}% staying in the business.`
    : '';

  if (!warnEl) return;
  let warning = '';
  if (streams.length === 0) {
    warning = 'No revenue streams — the simulation will show $0 revenue. Add at least one.';
  } else if (total === 0) {
    warning = 'Every stream is $0/month, so the simulation will show no revenue.';
  } else if (sharePct === 0) {
    warning = 'All profit is being paid out personally, so the business never builds cash and will run down its balance.';
  }
  warnEl.textContent = warning;
  warnEl.style.display = warning ? 'block' : 'none';
}

async function suggestNewStream() {
  const btn = $v('vizSuggestStreamBtn');
  const noteEl = $v('vizStreamSuggestNote');
  if (noteEl) { noteEl.style.display = 'none'; noteEl.textContent = ''; }
  if (btn) { btn.disabled = true; btn.textContent = 'Thinking…'; }

  try {
    const result = await vizPostJSON('/api/visualization/suggest-stream', {
      idea: VizState.idea,
      existingStreams: VizState.streams.map((s) => ({ name: s.name })),
    });

    const suggestedName = (result.name || '').trim();
    const duplicate = VizState.streams.some(
      (s) => s.name.trim().toLowerCase() === suggestedName.toLowerCase()
    );

    addStreamRow({
      name: suggestedName || 'Suggested stream',
      monthlyAmount: result.monthlyAmount,
      growthRatePct: result.growthRatePct,
      reinvest: true,
      origin: 'llm',
    });

    if (noteEl) {
      noteEl.textContent = duplicate
        ? `"${suggestedName}" is already in your list — added anyway, rename or remove it if it's a repeat.`
        : (result.reasoning || '');
      noteEl.style.display = noteEl.textContent ? 'block' : 'none';
    }
  } catch (err) {
    // Was console.error only, so this read as a dead button.
    console.error('Suggest-stream failed:', err.message);
    if (noteEl) {
      noteEl.textContent = `Couldn't get a suggestion (${err.message}). Add a stream manually instead.`;
      noteEl.style.display = 'block';
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '✨ Suggest a new stream'; }
  }
}

/* ==================== PANEL MARKUP ==================== */

function vizPanelHTML() {
  return `
    ${renderOriginKey()}

    <div class="viz-field">
      <label for="vizInitialInvestment">Personal investment ($)</label>
      <input type="number" id="vizInitialInvestment" placeholder="e.g. 5000" />
    </div>

    <div class="viz-field">
      <label for="vizStartupCost">One-time startup cost ($)</label>
      <input type="number" id="vizStartupCost" />
      <div class="hint">Equipment, deposits, licences, initial inventory — spent once, out of your investment, before month 1. Deducted for tax under section 195: $5,000 immediately, the rest spread over 180 months.</div>
    </div>

    <div class="viz-field">
      <label for="vizMonthsToForecast">Months to forecast</label>
      <input type="number" id="vizMonthsToForecast" placeholder="e.g. 12" />
    </div>

    <div class="viz-field">
      <label for="vizMonthlyPersonalExpenses">Monthly personal expenses ($)</label>
      <input type="number" id="vizMonthlyPersonalExpenses" placeholder="e.g. 2500" />
      <div class="hint">What you need to live on. The business pays this out as an owner draw when it can afford to, and the simulation flags the months it can't.</div>
    </div>

    <div class="viz-field">
      <label for="vizSector">Sector</label>
      <input type="text" id="vizSector" />
    </div>

    <div class="viz-field">
      <label for="vizCollectionLag">Collection lag (months)</label>
      <input type="number" id="vizCollectionLag" min="0" max="3" step="1" />
      <div class="hint">0 if you're paid at the point of sale. 1 for net-30 invoicing, 2 for net-60. Work is costed when performed; cash arrives this many months later.</div>
    </div>

    <div class="viz-field">
      <label for="vizLoanAmount">Loan amount (optional)</label>
      <input type="number" id="vizLoanAmount" placeholder="0" />
    </div>

    <div class="viz-field" id="vizLoanTypeField" style="display:none;">
      <label for="vizLoanType">Loan type</label>
      <select id="vizLoanType">
        <option value="bank">Bank loan</option>
        <option value="brokerage">Investment brokerage loan</option>
      </select>
      <div class="viz-loading" id="vizLoanLoading" style="display:none;">Getting loan terms…</div>
      <div class="viz-loading" id="vizLoanWarning" style="display:none; color: var(--red);"></div>
    </div>

    <div class="viz-field">
      <label for="vizStopLoss">Stop-loss threshold ($)</label>
      <input type="number" id="vizStopLoss" />
      <div class="hint">Zero or negative. The cash floor at which the simulation stops and calls it.</div>
    </div>

    <div class="viz-field">
      <label for="vizCushionFund">Cushion fund amount ($)</label>
      <input type="number" id="vizCushionFund" />
    </div>

    <div class="viz-field">
      <label for="vizCostPerService">Estimated price per service/item ($)</label>
      <input type="number" id="vizCostPerService" />
    </div>

    <!-- ---- Revenue streams: repeatable, editable, LLM can suggest more ---- -->
    <div class="viz-field">
      <label>Revenue streams</label>
      <div class="viz-stream-header">
        <span>Name</span><span>$/month</span><span>Growth %</span><span>Reinvest?</span><span></span>
      </div>
      <div id="vizStreamsContainer"></div>
      <div class="viz-stream-actions">
        <button type="button" class="ghost" id="vizAddStreamBtn">+ Add revenue stream</button>
        <button type="button" class="ghost" id="vizSuggestStreamBtn">✨ Suggest a new stream</button>
      </div>
      <div id="vizStreamTotals" class="viz-stream-totals"></div>
      <div id="vizStreamSuggestNote" class="viz-stream-note" style="display:none;"></div>
      <div id="vizStreamWarning" class="viz-stream-warning" style="display:none;"></div>
      <div class="hint">Leave "Reinvest" checked to keep that stream's profit in the business. Uncheck it and the profit goes straight to you instead — it still counts toward revenue and tax.</div>
    </div>

    <div class="viz-field">
      <label for="vizRevenueCeiling">Revenue ceiling — market saturation cap ($)</label>
      <input type="number" id="vizRevenueCeiling" />
      <div class="hint">Growth bends asymptotically toward this figure rather than stopping dead at it. Month 1 is unaffected.</div>
    </div>

    <div class="viz-field">
      <label for="vizFixedExpenses">Fixed expenses / month ($)</label>
      <input type="number" id="vizFixedExpenses" />
    </div>

    <div class="viz-field">
      <label for="vizFixedStepThreshold">Fixed cost step-up threshold (% revenue growth)</label>
      <input type="number" id="vizFixedStepThreshold" step="1" />
      <div class="hint">Every time revenue grows this % past its starting level, fixed costs jump up (hiring, bigger space). Step-ups ratchet — they don't reverse if revenue dips.</div>
    </div>

    <div class="viz-field">
      <label for="vizFixedStepAmount">Fixed cost step-up amount ($)</label>
      <input type="number" id="vizFixedStepAmount" />
    </div>

    <div class="viz-field">
      <label for="vizVariableExpenses">Variable expenses / month ($)</label>
      <input type="number" id="vizVariableExpenses" />
    </div>

    <div class="viz-field">
      <label for="vizTaxRate">Estimated tax rate (%)</label>
      <input type="number" id="vizTaxRate" step="0.1" />
      <div class="hint">Blended effective rate including self-employment tax. Can't be below 15.3%. Paid quarterly, one month after each quarter closes, with losses carried forward.</div>
    </div>

    <div id="vizReasoning" class="viz-reasoning"></div>
    <div id="vizPlausibility" class="viz-plausibility" style="display:none;"></div>
    <div id="vizLoading" class="viz-loading" style="display:none;">Estimating starting numbers…</div>

    <button class="primary" id="vizConfirmBtn" style="margin-top:18px;">Confirm and run simulation</button>
    <div id="vizSimResult" class="viz-reasoning" style="margin-top:14px;"></div>
    <div id="vizGraphContainer" style="margin-top:10px;"></div>
  `;
}

/* ==================== LOAN ==================== */

// Mirrors visualization.py's FIXED_LOAN_RATES_PCT / _amortized_payment. Used
// ONLY as a client-side fallback if the server call fails, so a loan never
// silently simulates as interest-free because a request dropped.
const FALLBACK_FIXED_LOAN_RATES_PCT = { bank: 7.5, brokerage: 11.0 };
const FALLBACK_LOAN_TERM_MONTHS = 60;

function fallbackAmortizedPayment(principal, annualRatePct, termMonths) {
  const monthlyRate = (annualRatePct / 100) / 12;
  if (monthlyRate === 0) return principal / termMonths;
  return (principal * monthlyRate) / (1 - Math.pow(1 + monthlyRate, -termMonths));
}

function clearLoanTerms() {
  VizState.loan.interestRatePct = null;
  VizState.loan.termMonths = null;
  VizState.loan.monthlyPayment = null;
  VizState.loan.isFallbackEstimate = false;
  const warnEl = $v('vizLoanWarning');
  if (warnEl) warnEl.style.display = 'none';
}

function bindLoanBehavior() {
  const amountEl = $v('vizLoanAmount');
  const typeFieldEl = $v('vizLoanTypeField');
  const typeSelectEl = $v('vizLoanType');
  if (!amountEl || !typeFieldEl || !typeSelectEl) return;

  amountEl.addEventListener('input', () => {
    const amount = streamNum(amountEl.value, 0);
    VizState.loan.amount = amount > 0 ? amount : null;
    typeFieldEl.style.display = amount > 0 ? 'block' : 'none';
    if (amount > 0 && typeSelectEl.value) {
      fetchLoanTerms(amount, typeSelectEl.value);
    } else {
      // Loan amount cleared — drop stale terms so old numbers don't linger.
      clearLoanTerms();
      recomputeSimulation();
    }
  });

  typeSelectEl.addEventListener('change', () => {
    VizState.loan.type = typeSelectEl.value;
    const amount = streamNum(amountEl.value, 0);
    if (amount > 0) fetchLoanTerms(amount, typeSelectEl.value);
  });
}

/**
 * Fetches loan terms and stores the in-flight promise on
 * VizState.loan.pendingFetch, which bindConfirmButton awaits before gathering
 * inputs — otherwise confirming right after typing a loan amount could
 * snapshot a still-null rate and payment.
 *
 * A failed request falls back to a local estimate using the same fixed-rate
 * table and amortization formula as the backend, plus a visible warning.
 * Previously the error was only console.error'd and the rate stayed null, so
 * `VizState.loan.monthlyPayment || 0` made a real loan simulate as
 * interest-free with no required payment — free money, silently.
 */
function fetchLoanTerms(amount, type) {
  const loadingEl = $v('vizLoanLoading');
  const warnEl = $v('vizLoanWarning');
  if (loadingEl) loadingEl.style.display = 'block';
  if (warnEl) warnEl.style.display = 'none';
  const confirmBtn = $v('vizConfirmBtn');
  if (confirmBtn) confirmBtn.disabled = true;

  const promise = vizPostJSON('/api/visualization/loan-terms', { loanAmount: amount, loanType: type })
    .then((result) => {
      VizState.loan.interestRatePct = streamNum(result.interestRatePct, 0);
      VizState.loan.termMonths = streamNum(result.termMonths, FALLBACK_LOAN_TERM_MONTHS);
      VizState.loan.monthlyPayment = streamNum(result.monthlyPayment, 0);
      VizState.loan.isFallbackEstimate = false;
      recomputeSimulation();
    })
    .catch((err) => {
      console.error('Loan terms estimate failed, using local fallback:', err.message);
      const rate = FALLBACK_FIXED_LOAN_RATES_PCT[type] ?? FALLBACK_FIXED_LOAN_RATES_PCT.bank;
      const termMonths = FALLBACK_LOAN_TERM_MONTHS;
      VizState.loan.interestRatePct = rate;
      VizState.loan.termMonths = termMonths;
      VizState.loan.monthlyPayment = Math.round(fallbackAmortizedPayment(amount, rate, termMonths) * 100) / 100;
      VizState.loan.isFallbackEstimate = true;
      if (warnEl) {
        warnEl.textContent = `Couldn't reach the loan-terms estimator — using a fallback ${rate}% / ${termMonths}mo estimate instead of skipping loan cost entirely.`;
        warnEl.style.display = 'block';
      }
      recomputeSimulation();
    })
    .finally(() => {
      if (loadingEl) loadingEl.style.display = 'none';
      if (confirmBtn) confirmBtn.disabled = false;
      VizState.loan.pendingFetch = null;
    });

  VizState.loan.pendingFetch = promise;
  return promise;
}

/* ==================== LLM ESTIMATES ==================== */

const ESTIMATE_FIELD_MAP = {
  sector: 'vizSector',
  stopLossThreshold: 'vizStopLoss',
  cushionFundAmount: 'vizCushionFund',
  costPerService: 'vizCostPerService',
  revenueCeiling: 'vizRevenueCeiling',
  fixedExpensesMonthly: 'vizFixedExpenses',
  fixedExpenseStepThresholdPct: 'vizFixedStepThreshold',
  fixedExpenseStepAmount: 'vizFixedStepAmount',
  variableExpensesMonthly: 'vizVariableExpenses',
  estimatedTaxRatePct: 'vizTaxRate',
  oneTimeStartupCost: 'vizStartupCost',
  collectionLagMonths: 'vizCollectionLag',
};

/**
 * Renders the backend's plausibility review. Built with DOM nodes rather than
 * innerHTML because these strings contain model-supplied field values.
 */
function renderPlausibility(plausibility) {
  const el = $v('vizPlausibility');
  if (!el) return;
  el.textContent = '';

  const issues = (plausibility && plausibility.issues) || [];
  if (!issues.length) {
    el.style.display = 'none';
    return;
  }

  const head = document.createElement('div');
  head.className = 'viz-plausibility-head';
  head.textContent = plausibility.summary || 'Some estimates needed review.';
  el.appendChild(head);

  const list = document.createElement('ul');
  issues.forEach((issue) => {
    const li = document.createElement('li');
    li.className = issue.severity === 'error' ? 'error' : 'warning';

    const name = document.createElement('strong');
    name.textContent = issue.field;
    li.appendChild(name);

    li.appendChild(document.createTextNode(
      issue.severity === 'error'
        ? ` — corrected ${issue.original} → ${issue.corrected}`
        : ' — looks unusual'
    ));

    const why = document.createElement('div');
    why.textContent = issue.message;
    li.appendChild(why);

    list.appendChild(li);
  });
  el.appendChild(list);
  el.style.display = 'block';
}

async function fetchEstimates() {
  const loadingEl = $v('vizLoading');
  if (loadingEl) loadingEl.style.display = 'block';

  try {
    const result = await vizPostJSON('/api/visualization/estimate', {
      idea: VizState.idea,
      sentiment: VizState.sentiment,
    });

    const originMap = result.origin || {};

    Object.entries(ESTIMATE_FIELD_MAP).forEach(([key, fieldId]) => {
      const el = $v(fieldId);
      // `result[key] !== undefined` alone wasn't enough: an explicit JSON
      // null passes it (null !== undefined is true), writing "null" into the
      // field and tagging it as a model estimate.
      if (el && result[key] !== undefined && result[key] !== null) {
        el.value = result[key];
        // Use the origin the backend actually reported. Tagging everything
        // 'llm' meant hardcoded fallbacks and corrected values both appeared
        // in the yellow "LLM estimate" colour.
        setFieldOrigin(fieldId, originMap[key] || 'llm');
      }
    });

    // Seed the first revenue stream from the estimate's starting revenue.
    if (VizState.streams.length === 0) {
      addStreamRow({
        name: 'Primary',
        monthlyAmount: result.costPerMonth,
        growthRatePct: result.growthRatePct,
        reinvest: true,
        origin: originMap.costPerMonth || 'llm',
      });
    }

    const reasoningEl = $v('vizReasoning');
    if (reasoningEl) reasoningEl.textContent = result.reasoning || '';

    VizState.plausibility = result.plausibility || null;
    renderPlausibility(VizState.plausibility);

    if (result._warning) {
      console.error('Estimate sub-call failures:', result._errorDetail);
    }
  } catch (err) {
    const reasoningEl = $v('vizReasoning');
    if (reasoningEl) reasoningEl.textContent = `Could not generate estimates (${err.message}).`;
    renderPlausibility(null);
  } finally {
    if (loadingEl) loadingEl.style.display = 'none';
  }
}

/* ==================== SIMULATION ==================== */

function gatherSimulationInputs() {
  // Coerced through the engine's own helper. `parseFloat(x) || 0` turned a
  // blank field AND "$5,000" into 0 alike, silently zeroing real values.
  const num = (id, fallback = 0) => streamNum($v(id) ? $v(id).value : undefined, fallback);

  return {
    personalInvestment: num('vizInitialInvestment'),
    oneTimeStartupCost: num('vizStartupCost'),
    loanAmount: num('vizLoanAmount'),
    monthlyLoanPayment: streamNum(VizState.loan.monthlyPayment, 0),
    loanInterestRatePct: streamNum(VizState.loan.interestRatePct, 0),
    monthsToForecast: num('vizMonthsToForecast', 12) || 12,
    monthlyPersonalExpenses: num('vizMonthlyPersonalExpenses'),
    collectionLagMonths: num('vizCollectionLag'),
    stopLossThreshold: num('vizStopLoss'),
    revenueStreams: VizState.streams.map((s) => ({
      name: s.name,
      monthlyAmount: streamNum(s.monthlyAmount, 0),
      growthRatePct: streamNum(s.growthRatePct, 0),
      reinvest: s.reinvest,
    })),
    revenueCeiling: num('vizRevenueCeiling') || Infinity,
    fixedExpensesMonthly: num('vizFixedExpenses'),
    fixedExpenseStepThresholdPct: num('vizFixedStepThreshold'),
    fixedExpenseStepAmount: num('vizFixedStepAmount'),
    variableExpensesMonthly: num('vizVariableExpenses'),
    estimatedTaxRatePct: num('vizTaxRate'),
  };
}

function recomputeSimulation() {
  if (typeof runSimulation !== 'function') return;
  VizState.simulation = runSimulation(gatherSimulationInputs());
}

function bindSimulationRecompute() {
  const fieldIds = [
    'vizInitialInvestment', 'vizStartupCost', 'vizMonthsToForecast',
    'vizMonthlyPersonalExpenses', 'vizCollectionLag', 'vizLoanAmount',
    'vizStopLoss', 'vizCushionFund', 'vizCostPerService', 'vizRevenueCeiling',
    'vizFixedExpenses', 'vizFixedStepThreshold', 'vizFixedStepAmount',
    'vizVariableExpenses', 'vizTaxRate',
  ];
  fieldIds.forEach((id) => {
    const el = $v(id);
    if (el) el.addEventListener('input', recomputeSimulation);
  });

  const addBtn = $v('vizAddStreamBtn');
  if (addBtn) addBtn.addEventListener('click', () => addStreamRow());
  const suggestBtn = $v('vizSuggestStreamBtn');
  if (suggestBtn) suggestBtn.addEventListener('click', suggestNewStream);
}

function formatSimulationSummary(sim) {
  if (!sim) return '';
  const money = (n) => '$' + Math.round(Math.abs(n || 0)).toLocaleString();
  const parts = [];

  parts.push(
    sim.haltedAtMonth !== null && sim.haltedAtMonth !== undefined
      ? `Simulation halts at month ${sim.haltedAtMonth} — cash hit the stop-loss threshold.`
      : `Runs the full forecast window with no stop-loss halt.`
  );

  // Two distinct milestones. Reporting one number labelled "break-even" was
  // conflating "revenue covers monthly costs" with "stake recouped".
  parts.push(
    sim.operatingBreakEvenMonth
      ? `Operating break-even (revenue covers monthly costs) at month ${sim.operatingBreakEvenMonth}.`
      : `Revenue never covers monthly costs inside the window.`
  );
  parts.push(
    sim.paybackMonth
      ? `Payback (your own money earned back) at month ${sim.paybackMonth}.`
      : `Payback not reached inside the window.`
  );

  if (sim.haltedAtMonth) {
    parts.push(`No runway left — the business ran out of cash.`);
  } else if (sim.runwayMonths !== null && sim.runwayMonths !== undefined) {
    parts.push(`Runway: about ${sim.runwayMonths} more month(s) at the current burn rate.`);
  } else {
    parts.push(`Cash-positive at the end of the window — no burn to project a runway from.`);
  }

  if (sim.cumulativePersonalShortfall > 0) {
    parts.push(
      `Warning: the business couldn't cover your personal expenses in full — ` +
      `${money(sim.cumulativePersonalShortfall)} short across the window` +
      (sim.firstPersonalShortfallMonth ? `, starting month ${sim.firstPersonalShortfallMonth}.` : '.')
    );
  }

  if (VizState.loan.isFallbackEstimate) {
    parts.push(`Note: loan terms used a local fallback estimate, not a fetched quote.`);
  }

  return parts.join(' ');
}

/**
 * Confirm button — waits for any in-flight loan-terms fetch before gathering
 * inputs, so it's impossible to confirm with a still-null rate and payment.
 */
function bindConfirmButton() {
  const btn = $v('vizConfirmBtn');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    if (VizState.loan.pendingFetch) {
      btn.disabled = true;
      btn.textContent = 'Waiting on loan terms…';
      try {
        await VizState.loan.pendingFetch;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Confirm and run simulation';
      }
    }

    recomputeSimulation();

    const snapshot = {
      inputs: gatherSimulationInputs(),
      sector: $v('vizSector') ? $v('vizSector').value : '',
      cushionFundAmount: streamNum($v('vizCushionFund') ? $v('vizCushionFund').value : 0, 0),
      costPerService: streamNum($v('vizCostPerService') ? $v('vizCostPerService').value : 0, 0),
      loan: Object.assign({}, VizState.loan, { pendingFetch: null }),
      origin: Object.assign({}, VizState.origin),
      plausibility: VizState.plausibility,
      simulation: VizState.simulation,
    };

    if (typeof state !== 'undefined') {
      state.vizResult = snapshot;
    }

    const resultEl = $v('vizSimResult');
    if (resultEl) resultEl.textContent = formatSimulationSummary(snapshot.simulation);

    // The mount call lives in GraphViz.jsx because this file is a classic
    // script and can never contain JSX — a JSX syntax error here would stop
    // the entire file from loading.
    const graphContainer = $v('vizGraphContainer');
    if (graphContainer && typeof window.mountGraphViz === 'function' && snapshot.simulation) {
      window.mountGraphViz(graphContainer, snapshot.simulation, snapshot.inputs);
    }

    const status2 = document.getElementById('status2');
    if (status2) status2.classList.add('done');

    const round3Gate = document.getElementById('round3Gate');
    const round3Body = document.getElementById('round3Body');
    if (round3Gate) round3Gate.style.display = 'none';
    if (round3Body) round3Body.style.display = 'block';

    // CHANGE (MAIN integration): MAIN's Round 3 page has a "Use my Simulator
    // numbers" button that pulls from a confirmed simulation — reveal it now
    // that one exists.
    const useSimBtn = document.getElementById('useSimInTaxBtn');
    if (useSimBtn) useSimBtn.style.display = 'inline-block';

    if (typeof updateExportVisibility === 'function') updateExportVisibility();
  });
}

/* ==================== ENTRY POINT ==================== */

function vizInit(containerEl, idea, sentimentResult) {
  VizState.idea = idea;
  VizState.sentiment = sentimentResult;
  VizState.simulation = null;
  VizState.plausibility = null;
  VizState.streams = [];
  VizState.origin = {};
  clearLoanTerms();
  VizState.loan.amount = null;
  VizState.loan.type = null;
  VizState.loan.pendingFetch = null;
  streamIdCounter = 0;

  containerEl.innerHTML = vizPanelHTML();
  renderStreamRows();   // shows the empty-state warning before /estimate returns

  ['vizStopLoss', 'vizCushionFund', 'vizCostPerService', 'vizRevenueCeiling',
   'vizFixedExpenses', 'vizFixedStepThreshold', 'vizFixedStepAmount',
   'vizVariableExpenses', 'vizSector', 'vizTaxRate', 'vizStartupCost',
   'vizCollectionLag']
    .forEach(bindOverrideBehavior);

  bindLoanBehavior();
  bindSimulationRecompute();
  bindConfirmButton();

  setFieldOrigin('vizInitialInvestment', 'user');
  setFieldOrigin('vizMonthsToForecast', 'user');
  setFieldOrigin('vizMonthlyPersonalExpenses', 'user');

  fetchEstimates().then(recomputeSimulation);
}