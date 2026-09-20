/*
 * Business > Visualization — React graph (v5).
 *
 * Same star topology and same CSS classes as before, so style.css needs no
 * changes. The "Simulate forever" fast-forward button has been removed
 * entirely (per request) — it extended the graph past the computed
 * forecast by pushing fabricated month objects with no real simulation math
 * run on them, so cash/revenue/expenses all silently rendered as zero past
 * the forecast length (e.g. "goes to zero after month 12" with the default
 * 12-month forecast). Rather than keep patching that path, it's gone: the
 * graph now only ever shows genuinely-simulated months (Play/scrub over
 * `simulationResult.months`, whose length is controlled by the "Months to
 * forecast" input).
 *
 * Integration is unchanged — visualization.js calls
 * window.mountGraphViz(containerEl, simulationResult, inputs).
 */

const {
  useState,
  useRef,
  useEffect,
  useCallback
} = React;
function fmtMoney(n) {
  const v = Number.isFinite(n) ? n : 0;
  const sign = v < 0 ? '-' : '';
  return `${sign}$${Math.abs(Math.round(v)).toLocaleString()}`;
}
const NODE_POS = {
  revenue: {
    x: 90,
    y: 70
  },
  fixed: {
    x: 90,
    y: 190
  },
  variable: {
    x: 90,
    y: 310
  },
  loan: {
    x: 90,
    y: 380
  },
  hub: {
    x: 500,
    y: 210
  }
};
const TWEEN_MS = 450; // normal playback / scrubbing
const PLAY_INTERVAL_MS = 600;
function easeOutCubic(t) {
  return 1 - Math.pow(1 - t, 3);
}
const TWEEN_FIELDS = ['revenue', 'fixedPortion', 'variablePortion', 'loanPayment', 'businessCashBalance', 'personalCashBalance', 'netCashFlow'];

/**
 * Returns a "displayed" month whose TWEEN_FIELDS animate toward the target's
 * values. Crucially, an interrupted tween hands off from what's currently on
 * screen, not from the last target that happened to finish animating.
 */
function useTweenedMonth(targetMonth, durationMs = TWEEN_MS) {
  const [displayed, setDisplayed] = useState(targetMonth);
  const frameRef = useRef(null);
  const fromRef = useRef(targetMonth);
  const onScreenRef = useRef(targetMonth);
  useEffect(() => {
    if (!targetMonth) return undefined;
    const from = fromRef.current || targetMonth;
    const to = targetMonth;
    const start = performance.now();
    const duration = Math.max(1, durationMs);
    if (frameRef.current) cancelAnimationFrame(frameRef.current);
    function tick(now) {
      const t = Math.min(1, (now - start) / duration);
      const eased = easeOutCubic(t);
      const next = {
        ...to
      };
      TWEEN_FIELDS.forEach(key => {
        const a = from[key] ?? 0;
        const b = to[key] ?? 0;
        next[key] = a + (b - a) * eased;
      });
      onScreenRef.current = next;
      setDisplayed(next);
      if (t < 1) {
        frameRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = to;
      }
    }
    frameRef.current = requestAnimationFrame(tick);
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current);
      // Hand off from what's actually rendered so a fast sequence of month
      // changes reads as continuous motion instead of restarting each time.
      fromRef.current = onScreenRef.current || from;
    };
  }, [targetMonth, durationMs]);
  return displayed || targetMonth;
}
function StatusLine({
  month,
  isLast,
  breakEvenMonth,
  haltedAtMonth
}) {
  let text = '';
  if (isLast && haltedAtMonth) {
    text = `Halted — cash hit the stop-loss threshold at month ${haltedAtMonth}.`;
  } else if (month.loanDefaulted) {
    text = `Loan payment missed this month — the business couldn't afford it. Unpaid interest is capitalizing onto the balance.`;
  } else if (breakEvenMonth && month.month >= breakEvenMonth) {
    text = `Past break-even (month ${breakEvenMonth}).`;
  }
  return /*#__PURE__*/React.createElement("div", {
    className: "graph-status"
  }, text);
}
function GraphViz({
  simulationResult,
  inputs
}) {
  const [months, setMonths] = useState(simulationResult.months);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [haltedAtMonth, setHaltedAtMonth] = useState(simulationResult.haltedAtMonth);
  const timerRef = useRef(null);

  // Refs mirror state so the interval callbacks always read the CURRENT
  // array and index instead of a stale closure from the click render.
  const monthsRef = useRef(simulationResult.months);
  const indexRef = useRef(0);
  useEffect(() => {
    monthsRef.current = months;
  }, [months]);
  useEffect(() => {
    indexRef.current = currentIndex;
  }, [currentIndex]);
  const baselineRevenue = simulationResult.baselineRevenue;
  const saturationFloor = simulationResult.saturationFloor;
  const breakEvenMonth = simulationResult.breakEvenMonth;
  const currentMonth = months[currentIndex];
  const displayed = useTweenedMonth(currentMonth, TWEEN_MS);
  const goToIndex = useCallback(idx => {
    indexRef.current = idx;
    setCurrentIndex(idx);
  }, []);
  const stopPlaying = useCallback(() => {
    setPlaying(false);
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
  }, []);
  useEffect(() => stopPlaying, [stopPlaying]); // clear timer on unmount

  const handleScrub = e => {
    stopPlaying();
    // The slider is indexed by POSITION, not by month number, so a capped or
    // partial run can't desynchronise the two.
    goToIndex(parseInt(e.target.value, 10));
  };
  const handlePlayClick = () => {
    if (playing) {
      stopPlaying();
      return;
    }
    if (indexRef.current >= monthsRef.current.length - 1) goToIndex(0);
    setPlaying(true);
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      if (indexRef.current >= monthsRef.current.length - 1) {
        stopPlaying();
        return;
      }
      goToIndex(indexRef.current + 1);
    }, PLAY_INTERVAL_MS);
  };
  if (!currentMonth) return null;
  const p = NODE_POS;
  const isLast = currentIndex === months.length - 1;
  const label = `Month ${currentMonth.month} / ${months.length}`;
  return /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("svg", {
    viewBox: "0 0 700 420",
    className: "graph-svg"
  }, /*#__PURE__*/React.createElement(Edge, {
    id: "revenue",
    from: p.revenue,
    to: p.hub,
    value: displayed.revenue
  }), /*#__PURE__*/React.createElement(Edge, {
    id: "fixed",
    from: p.fixed,
    to: p.hub,
    value: displayed.fixedPortion
  }), /*#__PURE__*/React.createElement(Edge, {
    id: "variable",
    from: p.variable,
    to: p.hub,
    value: displayed.variablePortion
  }), /*#__PURE__*/React.createElement(Edge, {
    id: "loan",
    from: p.loan,
    to: p.hub,
    value: displayed.loanPayment,
    overrideLabel: currentMonth.loanDefaulted ? 'defaulted' : null
  }), /*#__PURE__*/React.createElement(Node, {
    id: "revenue",
    pos: p.revenue,
    label: "Revenue",
    colorClass: "node-revenue",
    value: displayed.revenue
  }), /*#__PURE__*/React.createElement(Node, {
    id: "fixed",
    pos: p.fixed,
    label: "Fixed Expenses",
    colorClass: "node-expense",
    value: displayed.fixedPortion
  }), /*#__PURE__*/React.createElement(Node, {
    id: "variable",
    pos: p.variable,
    label: "Variable Expenses",
    colorClass: "node-expense",
    value: displayed.variablePortion
  }), /*#__PURE__*/React.createElement(Node, {
    id: "loan",
    pos: p.loan,
    label: "Loan Payment",
    colorClass: "node-expense",
    value: displayed.loanPayment,
    suffix: currentMonth.loanDefaulted ? ' (missed)' : ''
  }), /*#__PURE__*/React.createElement("g", {
    className: "graph-node node-hub"
  }, /*#__PURE__*/React.createElement("rect", {
    x: p.hub.x - 110,
    y: p.hub.y - 65,
    width: "220",
    height: "150",
    rx: "14"
  }), /*#__PURE__*/React.createElement("text", {
    x: p.hub.x,
    y: p.hub.y - 38,
    className: "graph-node-label"
  }, "Central Hub"), /*#__PURE__*/React.createElement("text", {
    x: p.hub.x,
    y: p.hub.y - 16,
    className: "graph-node-value"
  }, fmtMoney(displayed.businessCashBalance)), /*#__PURE__*/React.createElement("text", {
    x: p.hub.x,
    y: p.hub.y + 4,
    className: "graph-node-sublabel"
  }, "Business cash balance"), /*#__PURE__*/React.createElement("text", {
    x: p.hub.x,
    y: p.hub.y + 24,
    className: "graph-node-sub"
  }, `Net: ${fmtMoney(displayed.netCashFlow)}/mo`), /*#__PURE__*/React.createElement("text", {
    x: p.hub.x,
    y: p.hub.y + 42,
    className: "graph-node-sub"
  }, `Personal: ${fmtMoney(displayed.personalCashBalance)}`), /*#__PURE__*/React.createElement("text", {
    x: p.hub.x,
    y: p.hub.y + 60,
    className: "graph-node-sub"
  }, currentMonth.taxPaid ? `Tax paid: ${fmtMoney(currentMonth.taxPaid)}` : `Reinvested: ${Math.round((currentMonth.reinvestShare ?? 1) * 100)}%`))), /*#__PURE__*/React.createElement("div", {
    className: "graph-controls"
  }, /*#__PURE__*/React.createElement("button", {
    className: "ghost",
    onClick: handlePlayClick
  }, playing ? '⏸ Pause' : '▶ Play'), /*#__PURE__*/React.createElement("input", {
    type: "range",
    min: "0",
    max: Math.max(0, months.length - 1),
    value: currentIndex,
    step: "1",
    onChange: handleScrub
  }), /*#__PURE__*/React.createElement("span", {
    className: "graph-month-label"
  }, label)), /*#__PURE__*/React.createElement(StatusLine, {
    month: currentMonth,
    isLast: isLast,
    breakEvenMonth: breakEvenMonth,
    haltedAtMonth: haltedAtMonth
  }));
}
function Node({
  id,
  pos,
  label,
  colorClass,
  value,
  suffix = ''
}) {
  return /*#__PURE__*/React.createElement("g", {
    className: `graph-node ${colorClass}`,
    "data-node": id
  }, /*#__PURE__*/React.createElement("rect", {
    x: pos.x - 85,
    y: pos.y - 32,
    width: "170",
    height: "64",
    rx: "10"
  }), /*#__PURE__*/React.createElement("text", {
    x: pos.x,
    y: pos.y - 6,
    className: "graph-node-label"
  }, label), /*#__PURE__*/React.createElement("text", {
    x: pos.x,
    y: pos.y + 16,
    className: "graph-node-value"
  }, fmtMoney(value) + suffix));
}
function Edge({
  id,
  from,
  to,
  value,
  overrideLabel = null
}) {
  return /*#__PURE__*/React.createElement("g", {
    className: "graph-edge",
    "data-edge": id
  }, /*#__PURE__*/React.createElement("line", {
    x1: from.x + 85,
    y1: from.y,
    x2: to.x - 95,
    y2: to.y
  }), /*#__PURE__*/React.createElement("text", {
    x: (from.x + to.x) / 2,
    y: (from.y + to.y) / 2 - 8,
    className: "graph-edge-label"
  }, overrideLabel ?? fmtMoney(value)));
}

/*
 * Plain-JS-callable mount point. visualization.js is a classic script and so
 * can never contain JSX; all JSX (including the mount) lives in this file,
 * which is the one Babel compiles.
 *
 * The bumped `key` forces a full remount on every Confirm, so a fresh
 * simulation always starts clean at month 1 rather than keeping the previous
 * instance's useState.
 */
function mountGraphViz(containerEl, simulationResult, inputs) {
  if (!containerEl) return;
  if (!containerEl._graphVizRoot) {
    containerEl._graphVizRoot = ReactDOM.createRoot(containerEl);
    containerEl._graphVizRunId = 0;
  }
  containerEl._graphVizRunId += 1;
  containerEl._graphVizRoot.render(/*#__PURE__*/React.createElement(GraphViz, {
    key: containerEl._graphVizRunId,
    simulationResult: simulationResult,
    inputs: inputs
  }));
}
window.GraphViz = GraphViz;
window.mountGraphViz = mountGraphViz;