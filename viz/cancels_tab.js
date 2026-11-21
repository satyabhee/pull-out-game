/* "Cancel matching" tab: abstract, stepwise diagrams of how the replay matches a contested cancel to a maker,
   and every traced way it fails. Data: analysis/cancel_trace.py over held66 (55 cases), Nov 20.
   Inlined into viz/index.html between the CANCELS markers by viz/build_cancels.py. */
(function () {
  const h = React.createElement, { useState, useEffect } = React;
  const RM = typeof matchMedia != "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DUR = RM ? 0 : 750, EASE = "cubic-bezier(.65,0,.35,1)";
  const K = { line: "#283343", ink: "#ebe5d8", mute: "#8d99aa", faint: "#4b5667", M: "#f08bc6", R: "#a891ff",
              N: "#7c8594", T: "#ffb84d", cancel: "#ff6f59", good: "#6fd88f", acc: "#f3ecdc" };
  const tr = (d = 0) => ({ transition: `transform ${DUR}ms ${EASE} ${d}ms, opacity ${DUR}ms ${EASE} ${d}ms` });

  // ---------- scene data ----------
  const STAGES = [
    ["Order", "block actions"],
    ["Read", "cloid or id"],
    ["Find", "the named order"],
    ["Remove", "it from the book"],
    ["Fill", "head of the queue"],
  ];
  const P = (...s) => s;                      // stage states: "", "on", "ok", "x", "skip"
  const scenes = [];

  scenes.push({
    id: "what", chip: "A contested cancel", title: "What a contested cancel looks like",
    show: { pipe: false, replay: false },
    steps: [
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: -1,
        chain: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]],
        cap: "A block is a list of actions. Here maker M cancels one order and posts a new one, and a taker trades at the same price, all inside one block." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: 0,
        chain: [["m1", "M", "c1", "gone"], ["r", "R"], ["n", "N"]],
        cap: "On chain, the cancel names M's order c1 and removes it from the queue." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: 1,
        chain: [["r", "R"], ["n", "N"], ["m2", "M", "c2"]],
        cap: "M's new post-only order c2 joins the back of the queue." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: 2,
        chain: [["r", "R", "", "hit"], ["n", "N"], ["m2", "M", "c2"]], chainFill: ["R", true],
        cap: "The taker fills the head of the queue, so R gets the fill. The trade is contested: a maker at that price acted in the same block, so who fills depends on which order the cancel named and on the order of actions inside the block." },
    ],
  });

  scenes.push({
    id: "process", chip: "How we match it", title: "How the replay matches a cancel to a maker",
    show: { pipe: true, replay: true },
    steps: [
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: -1, pipe: P("", "", "", "", ""),
        chain: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]], replay: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]],
        cap: "Same block, now replayed. The replay keeps its own copy of the queue and runs every action through five steps." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: -1, pipe: P("on", "", "", "", ""),
        chain: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]], replay: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]],
        cap: "1 · Order. Actions run in the order the block lists them." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: 0, pipe: P("ok", "on", "", "", ""),
        chain: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]], replay: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]],
        cap: "2 · Read. A cancel names its target one of two ways: the client's own label (cloid), or the exchange's order id." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: 0, pipe: P("ok", "ok", "on", "", ""), find: true,
        chain: [["m1", "M", "c1"], ["r", "R"], ["n", "N"]], replay: [["m1", "M", "c1", "hit"], ["r", "R"], ["n", "N"]],
        cap: "3 · Find. A cloid is looked up directly: (maker, coin, cloid) must hit exactly one resting order. An order id is never public for in-window orders, so the resolver estimates it: exact id if known, else 'placed before the window', else the bracket rule, else the nearest estimate within tolerance. If none is unique, it gives up." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: 1, pipe: P("ok", "ok", "ok", "on", ""),
        chain: [["r", "R"], ["n", "N"], ["m2", "M", "c2"]], replay: [["r", "R"], ["n", "N"], ["m2", "M", "c2"]],
        cap: "4 · Remove. The order found is taken off the replay's queue. The new post-only order is then added." },
      { block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]], at: 2, pipe: P("ok", "ok", "ok", "ok", "ok"),
        chain: [["r", "R", "", "hit"], ["n", "N"], ["m2", "M", "c2"]], replay: [["r", "R", "", "hit"], ["n", "N"], ["m2", "M", "c2"]],
        chainFill: ["R", true], replayFill: ["R", true],
        cap: "5 · Fill. The taker hits the head of each queue. Both credit R: a match. The next six tabs are every way this breaks in the traced cases." },
    ],
  });

  // a failure scenario = input, run to the failing stage, outcome
  function fail(o) {
    const pre = o.pass, n = STAGES.length;
    const st = (k, last) => Array.from({ length: n }, (_, i) => i < k ? "ok" : i === k ? last : "");
    const steps = [
      { block: o.block, at: -1, pipe: st(-1, ""), chain: o.chain0, replay: o.replay0, cap: "Input. " + o.input },
      ...o.run.map((r, i) => ({ block: o.block, at: r.at, pipe: st(r.stage, "on"), chain: r.chain || o.chain0,
                                 replay: r.replay || o.replay0, cap: r.cap, find: r.find })),
      { block: o.block, at: o.block.length - 1, pipe: (() => { const s = st(o.failAt, "x"); for (let i = o.failAt + 1; i < n; i++) s[i] = o.after || "skip"; return s; })(),
        chain: o.chainEnd, replay: o.replayEnd, chainFill: ["R", true], replayFill: [o.replayMaker || "M", false],
        cap: o.outcome, failNote: o.failNote },
    ];
    return { id: o.id, chip: o.chip, title: o.title, count: o.count, show: { pipe: true, replay: true }, steps, failAt: o.failAt };
  }

  scenes.push(fail({
    id: "after", chip: "Cancel after the taker", title: "The block lists the cancel after the taker", count: 21, failAt: 0, after: "ok",
    block: [["a", "M", "c2"], ["t", "T"], ["c", "M", "c1"]],
    chain0: [["m1", "M", "c1"], ["r", "R"]], replay0: [["m1", "M", "c1"], ["r", "R"]],
    input: "M's order c1 rests at the head. The block lists the taker before M's cancel.",
    run: [{ stage: 0, at: 1, cap: "1 · Order. Listing order runs the taker first, while c1 is still at the head." }],
    chainEnd: [["r", "R", "", "hit"], ["m2", "M", "c2"]], replayEnd: [["m1", "M", "c1", "hit"], ["r", "R"], ["m2", "M", "c2"]],
    outcome: "The replay fills M's c1. On chain R got the fill, which is what happens if cancels run before takers inside a block, the rule this project studies. Steps 2 to 4 work, but too late. A cancels-first order of actions (the class policy) would apply M's cancel first.",
    failNote: "wrong order of actions",
  }));

  scenes.push(fail({
    id: "other", chip: "Cancel hit another order", title: "The cancel worked, but M's other order took the fill", count: 17, failAt: 4,
    block: [["c", "M", "c1"], ["a", "M", "c2"], ["t", "T"]],
    chain0: [["m1", "M", "c1"], ["r", "R"]], replay0: [["m1", "M", "c1"], ["r", "R"]],
    input: "M cancels c1 and posts c2 in the same block, then a taker trades.",
    run: [
      { stage: 2, at: 0, find: true, replay: [["m1", "M", "c1", "hit"], ["r", "R"]], cap: "1 to 3 · The cloid lookup finds c1, exactly one order. Correct." },
      { stage: 3, at: 1, replay: [["m2", "M", "c2"], ["r", "R"]], chain: [["r", "R"], ["m2", "M", "c2"]],
        cap: "4 · c1 is removed and c2 is added. In the replay c2 ends up ahead of R; on chain R stayed ahead." },
    ],
    chainEnd: [["r", "R", "", "hit"], ["m2", "M", "c2"]], replayEnd: [["m2", "M", "c2", "hit"], ["r", "R"]],
    outcome: "The cancel was matched correctly. The miss is the queue position of M's new order. In 11 of the 17, the order that filled was a post-only from the same block.",
    failNote: "new order placed ahead of R",
  }));

  scenes.push(fail({
    id: "gone", chip: "Cloid already gone", title: "The cloid names an order the replay already removed", count: 8, failAt: 2,
    block: [["c", "M", "c1"], ["t", "T"]],
    chain0: [["m1", "M", "c1"], ["r", "R"]], replay0: [["m1", "M", "c1", "ghost"], ["m3", "M", "c3"], ["r", "R"]],
    input: "On chain c1 still rests. The replay already took c1 off earlier (filled or trimmed), and holds another M order, c3.",
    run: [{ stage: 2, at: 0, find: true, cap: "3 · Find. No resting order has cloid c1, so the lookup returns nothing." }],
    chainEnd: [["r", "R", "", "hit"]], replayEnd: [["m3", "M", "c3", "hit"], ["r", "R"]],
    outcome: "Nothing is removed. M's other order c3 sits at the head and takes the fill; on chain R did. The replay's queue had already drifted before this block.",
    failNote: "no order with that cloid",
  }));

  scenes.push(fail({
    id: "wrongid", chip: "Id matched wrong order", title: "The id resolver picks the wrong order", count: 6, failAt: 2,
    block: [["c", "M", "id"], ["t", "T"]],
    chain0: [["m1", "M", "o1"], ["m2", "M", "o2"], ["r", "R"]], replay0: [["m1", "M", "o1"], ["m2", "M", "o2"], ["r", "R"]],
    input: "M cancels by order id. The replay holds two of M's orders, o1 and o2, with estimated ids.",
    run: [{ stage: 2, at: 0, find: true, replay: [["m1", "M", "o1"], ["m2", "M", "o2", "hit"], ["r", "R"]],
            cap: "3 · Find. The estimate lands nearest o2, so the resolver removes o2. The order that then fills, o1, is another of M's orders." }],
    chainEnd: [["r", "R", "", "hit"]], replayEnd: [["m1", "M", "o1", "hit"], ["r", "R"]],
    outcome: "o1 stays at the head and fills, so the replay credits M; the chain credited R. Most likely the cancel was meant for o1.",
    failNote: "resolved to the wrong order",
  }));

  scenes.push(fail({
    id: "rejected", chip: "Cloid never rested", title: "The cloid names an order the replay rejected", count: 2, failAt: 2,
    block: [["c", "M", "c1"], ["t", "T"]],
    chain0: [["m1", "M", "c1"], ["r", "R"]], replay0: [["m1", "M", "c1", "ghost"], ["m3", "M", "c3"], ["r", "R"]],
    input: "Earlier, the chain accepted M's post-only c1 but the replay's admission check rejected it.",
    run: [{ stage: 2, at: 0, find: true, cap: "3 · Find. c1 was never in the replay's book, so the lookup returns nothing." }],
    chainEnd: [["r", "R", "", "hit"]], replayEnd: [["m3", "M", "c3", "hit"], ["r", "R"]],
    outcome: "Nothing is removed, and M's other order c3 fills in the replay. The mistake happened earlier, at admission, not at the cancel.",
    failNote: "order never admitted",
  }));

  scenes.push(fail({
    id: "exactgone", chip: "Exact id, order gone", title: "The exact id is known, but the order is gone", count: 1, failAt: 2,
    block: [["c", "M", "id"], ["t", "T"]],
    chain0: [["m1", "M", "o1"], ["r", "R"]], replay0: [["m1", "M", "o1", "ghost"], ["m3", "M", "o3"], ["r", "R"]],
    input: "Public data gives this order's exact id, but the replay already removed it.",
    run: [{ stage: 2, at: 0, find: true, cap: "3 · Find. The exact id maps to an order no longer resting. The resolver never guesses another one." }],
    chainEnd: [["r", "R", "", "hit"]], replayEnd: [["m3", "M", "o3", "hit"], ["r", "R"]],
    outcome: "Nothing is removed and M's other order fills. Like the cloid case, the replay's queue had already drifted.",
    failNote: "target already gone",
  }));

  const FAILS = scenes.filter(s => s.count), TOTAL = FAILS.reduce((a, s) => a + s.count, 0);

  // ---------- drawing ----------
  const TW = 66, TH = 40, GAP = 12;
  function Tile({ who, sub, mode }) {
    const c = K[who] || K.N, ghost = mode === "ghost", hit = mode === "hit";
    return h("g", { opacity: mode === "gone" ? 0 : 1, style: tr() },
      h("rect", { x: 0, y: 0, width: TW, height: TH, rx: 7, fill: c, fillOpacity: ghost ? 0.02 : 0.16, stroke: c,
                  strokeWidth: hit ? 2.8 : 1.4, strokeDasharray: ghost ? "4 4" : undefined, strokeOpacity: ghost ? 0.6 : 1 }),
      h("text", { x: 11, y: 25, className: "cx-tl", fill: c, opacity: ghost ? 0.55 : 1 }, who === "N" ? "" : who),
      sub ? h("text", { x: TW - 10, y: 25, className: "cx-ts", textAnchor: "end", opacity: ghost ? 0.5 : 1 }, sub) : null);
  }

  function Queue({ x, y, items, label, fill, hide }) {
    return h("g", { opacity: hide ? 0 : 1, style: tr() },
      h("text", { x, y: y - 14, className: "cx-lbl" }, label),
      h("line", { x1: x - 8, y1: y + TH + 10, x2: x + 4 * (TW + GAP), y2: y + TH + 10, stroke: K.line }),
      items.map(([id, who, sub, mode], i) =>
        h("g", { key: id, style: { transform: `translate(${x + i * (TW + GAP)}px, ${y}px)`, ...tr() } },
          h(Tile, { who, sub, mode }))),
      fill ? h("g", { style: tr() },
        h("text", { x: x + 4 * (TW + GAP) + 6, y: y + 26, className: "cx-fill", fill: fill[1] ? K.good : K.cancel },
          (fill[1] ? "✓ " : "✕ ") + "fill → " + fill[0])) : null);
  }

  function Glyph({ k, who, sub, state }) {
    const c = k === "t" ? K.T : k === "c" ? K.cancel : K[who];
    const dim = state === "todo" ? 0.35 : 1, cur = state === "now";
    let shape;
    if (k === "t") shape = h("path", { d: "M8 6 L40 22 L8 38 Z", fill: K.T, fillOpacity: 0.2, stroke: K.T, strokeWidth: cur ? 2.6 : 1.4 });
    else if (k === "c") shape = h("g", null,
      h("rect", { x: 2, y: 4, width: 40, height: 36, rx: 6, fill: "none", stroke: K.M, strokeWidth: cur ? 2.6 : 1.4 }),
      h("path", { d: "M12 12 L32 32 M32 12 L12 32", stroke: K.cancel, strokeWidth: 2.4, strokeLinecap: "round" }));
    else shape = h("rect", { x: 2, y: 4, width: 40, height: 36, rx: 6, fill: K.M, fillOpacity: 0.16, stroke: K.M, strokeWidth: cur ? 2.6 : 1.4 });
    const word = k === "t" ? "taker" : k === "c" ? "cancel " + sub : "post-only " + sub;
    return h("g", { opacity: dim, style: tr() }, shape,
      h("text", { x: 22, y: 60, className: "cx-gl", textAnchor: "middle", fill: k === "t" ? K.T : K.ink }, word),
      k !== "t" ? h("text", { x: 22, y: 76, className: "cx-gw", textAnchor: "middle", fill: K.M }, "M") : null);
  }

  function Block({ items, at }) {
    const x0 = 40;
    return h("g", null,
      h("text", { x: x0, y: 24, className: "cx-lbl" }, "BLOCK · listing order"),
      h("line", { x1: x0, y1: 62, x2: x0 + items.length * 120 - 60, y2: 62, stroke: K.line, strokeDasharray: "2 6" }),
      items.map(([k, who, sub], i) => h("g", { key: k + i, style: { transform: `translate(${x0 + i * 120}px, 40px)`, ...tr() } },
        h(Glyph, { k, who, sub, state: at < 0 ? "todo" : i < at ? "done" : i === at ? "now" : "todo" }))));
  }

  function Pipe({ states, failNote, find }) {
    const x0 = 40, w = 164, gap = 22, y = 168;
    return h("g", null,
      STAGES.map(([name, sub], i) => {
        const s = states[i] || "", x = x0 + i * (w + gap);
        const col = s === "ok" ? K.good : s === "x" ? K.cancel : s === "on" ? K.acc : K.faint;
        return h("g", { key: name },
          i > 0 ? h("line", { x1: x - gap, y1: y + 30, x2: x, y2: y + 30, stroke: states[i - 1] === "ok" ? K.good : K.line, strokeWidth: 1.6, style: tr() }) : null,
          h("rect", { x, y, width: w, height: 60, rx: 10, fill: col, fillOpacity: s === "on" ? 0.12 : s ? 0.08 : 0,
                      stroke: col, strokeWidth: s === "on" || s === "x" ? 2.4 : 1.2, strokeDasharray: s === "skip" ? "3 5" : undefined, style: tr() }),
          h("text", { x: x + 14, y: y + 26, className: "cx-sn", fill: s ? col : K.mute }, `${i + 1}  ${name}`),
          h("text", { x: x + 14, y: y + 45, className: "cx-ss" }, sub),
          s === "x" ? h("g", null,
            h("text", { x: x + w - 14, y: y + 27, className: "cx-x", textAnchor: "end", fill: K.cancel }, "✕"),
            failNote ? h("text", { x: x + w / 2, y: y + 84, className: "cx-fn", textAnchor: "middle", fill: K.cancel }, failNote) : null) : null,
          i === 2 && find ? h("g", null,
            h("text", { x: x + w / 2, y: y + 84, className: "cx-ss", textAnchor: "middle" }, "cloid → table"),
            h("text", { x: x + w / 2, y: y + 100, className: "cx-ss", textAnchor: "middle" }, "id → resolver")) : null);
      }));
  }

  function Stage({ sc, step }) {
    const s = sc.steps[step], showPipe = sc.show.pipe, showReplay = sc.show.replay;
    const qy = showPipe ? 316 : 170;
    return h("svg", { viewBox: `0 0 960 ${showPipe ? 400 : 260}`, className: "cx-svg", role: "img", "aria-label": sc.title },
      h(Block, { items: s.block, at: s.at }),
      showPipe ? h(Pipe, { states: s.pipe || [], failNote: s.failNote, find: s.find && !s.failNote }) : null,
      h(Queue, { x: 40, y: qy, items: s.chain, label: "CHAIN · queue at the price", fill: s.chainFill }),
      showReplay ? h(Queue, { x: 520, y: qy, items: s.replay || [], label: "REPLAY · same queue", fill: s.replayFill }) : null);
  }

  function Breakdown({ cur, go }) {
    let acc = 0;
    return h("div", { className: "cx-bd" },
      h("div", { className: "cx-bar" }, FAILS.map(s => {
        const w = (100 * s.count) / TOTAL; acc += w;
        return h("button", { key: s.id, className: "cx-seg" + (cur === s.id ? " on" : ""), style: { flexBasis: w + "%" },
                             onClick: () => go(s.id), title: `${s.chip}: ${s.count} of ${TOTAL}` }, s.count);
      })),
      h("p", { className: "cx-bdcap" }, `${TOTAL} traced misses where the maker the replay credited cancelled in the same block (66 held-out chunks). Click a segment.`));
  }

  function CancelView() {
    const [si, setSi] = useState(0), [step, setStep] = useState(0);
    const sc = scenes[si], last = sc.steps.length - 1;
    const go = id => { setSi(scenes.findIndex(s => s.id === id)); setStep(0); };
    useEffect(() => {
      const k = e => {
        if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
        if (e.key === "ArrowRight") { if (step < last) setStep(step + 1); else if (si < scenes.length - 1) { setSi(si + 1); setStep(0); } }
        if (e.key === "ArrowLeft") { if (step > 0) setStep(step - 1); else if (si > 0) { setSi(si - 1); setStep(scenes[si - 1].steps.length - 1); } }
      };
      window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k);
    }, [si, step, last]);
    return h("div", { className: "cx" },
      h("header", { className: "cx-head" },
        h("h1", null, "Matching contested cancels"),
        h("p", null, "How the replay decides which order a cancel removes, and the six ways it ends up crediting the wrong maker.")),
      h("nav", { className: "cx-chips", "aria-label": "Scenes" }, scenes.map((s, i) =>
        h("button", { key: s.id, className: "cx-chip" + (i === si ? " on" : "") + (s.count ? " f" : ""), onClick: () => { setSi(i); setStep(0); } },
          s.count ? h("span", { className: "cx-n" }, s.count) : null, s.chip))),
      FAILS.some(f => f.id === sc.id) || sc.id === "process" ? h(Breakdown, { cur: sc.id, go }) : null,
      h("h2", { className: "cx-title" }, sc.title, sc.count ? h("span", { className: "cx-cnt" }, ` · ${sc.count} of ${TOTAL}`) : null),
      h("div", { className: "cx-stage" }, h(Stage, { sc, step })),
      h("div", { className: "cx-foot" },
        h("p", { className: "cx-cap", "aria-live": "polite" }, sc.steps[step].cap),
        h("div", { className: "cx-ctl" },
          h("button", { onClick: () => setStep(Math.max(0, step - 1)), disabled: step === 0 }, "← Back"),
          h("span", { className: "cx-dots" }, sc.steps.map((_, i) => h("i", { key: i, className: i === step ? "on" : "", onClick: () => setStep(i) }))),
          h("button", { className: "pri", onClick: () => step < last ? setStep(step + 1) : si < scenes.length - 1 && (setSi(si + 1), setStep(0)) },
            step < last ? "Next step →" : si < scenes.length - 1 ? "Next scene →" : "Done"))),
      h("div", { className: "cx-legend" },
        h("span", null, h("i", { style: { borderColor: K.M } }), "M · maker the replay credits"),
        h("span", null, h("i", { style: { borderColor: K.R } }), "R · maker the chain credits"),
        h("span", null, h("i", { style: { borderColor: K.N } }), "other makers"),
        h("span", null, h("i", { style: { borderColor: K.M, borderStyle: "dashed" } }), "order the replay no longer holds"),
        h("span", { className: "cx-src" }, "Data: analysis/cancel_trace.py, held66, commit 'user_orders: parse without caching'")));
  }
  window.CancelView = CancelView;
})();
