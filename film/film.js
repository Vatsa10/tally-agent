// The tallyagent film, drawn as a pure function of time.
//
// Every frame is render(t): nothing animates on its own, so the renderer can
// step through the film one frame at a time and every frame is exact. Every
// line a terminal "prints" here is a line the product printed, captured live
// by scripts/film_capture.py into CAPTURE; scene lengths come from the
// narration in TIMING. Scenes marked with a clip leave a framed window where
// the real TallyPrime recording is composited in afterwards (render.mjs).

const D = window.CAPTURE;
const S = window.STORIES;
const T = window.TIMING;
const stage = document.getElementById("stage");

// --- motion ---------------------------------------------------------------------

const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const lerp = (a, b, x) => a + (b - a) * x;
// Expo-out: fast start, long soft landing - the curve that reads as "smooth".
const easeOut = (x) => (x >= 1 ? 1 : 1 - Math.pow(2, -10 * clamp(x)));
const easeInOut = (x) => {
  x = clamp(x);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const prog = (t, at, over) => easeOut((t - at) / over);

// The standard entrance: rise, sharpen and fade in.
function enter(t, at, { dy = 28, over = 0.9, blur = 8, scale = 0.985 } = {}) {
  const p = prog(t, at, over);
  return `opacity:${p};transform:translateY(${(1 - p) * dy}px) scale(${lerp(scale, 1, p)});filter:blur(${(1 - p) * blur}px)`;
}

// A number that counts up to its value, with the value's own formatting.
function count(t, at, target, over = 1.6, decimals = 0) {
  const p = easeOut((t - at) / over);
  return (target * p).toLocaleString("en-IN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function typed(text, t, at, cps = 34) {
  const n = Math.floor(clamp((t - at) * cps, 0, text.length));
  return { text: text.slice(0, n), done: n >= text.length, typing: t >= at && n < text.length };
}

const caret = (t, on) => (on || Math.floor(t * 2.2) % 2 === 0 ? '<span class="caret"></span>' : "");

// --- data ---------------------------------------------------------------------

const scene = (id) => D.scenes[id] || [];
const turn = (id, i) => scene(id)[i] || { input: "", lines: [], seconds: 0 };
const lineOf = (id, i, kind) => (turn(id, i).lines.find((l) => l.kind === kind) || {}).text || "";
const money = (s) => String(s).replace(/₹/g, "Rs ");
const md = (html) => html.replace(/\*\*(.+?)\*\*/g, '<span class="hl">$1</span>');

// --- building blocks --------------------------------------------------------------

// A terminal that types its input, then slides each output line in.
function terminal({ x, y, w, h, title, status = "", blocks, t, at = 0 }) {
  let body = "";
  for (const b of blocks) {
    if (t < b.at) break;
    if (b.input !== undefined) {
      const ty = typed(b.input, t, b.at, b.cps || 38);
      body += `<div class="k-user">${esc(ty.text)}${ty.done ? "" : caret(t, ty.typing)}</div>`;
      if (!ty.done) break;
      continue;
    }
    const every = b.every ?? 0.32;
    (b.lines || []).forEach((l, i) => {
      const lineAt = b.at + i * every;
      if (t >= lineAt) body += `<div style="${enter(t, lineAt, { dy: 10, over: 0.45, blur: 3, scale: 1 })}">${l}</div>`;
    });
  }
  return `<div class="term" style="left:${x}px;top:${y}px;width:${w}px;height:${h}px;${enter(t, at, { dy: 40, over: 1 })}">
    <div class="bar"><span class="dot"></span><span class="dot"></span><span class="dot"></span>
    <span class="title">${esc(title)}</span><span class="status">${esc(status)}</span></div>
    <div class="body">${body}</div></div>`;
}

const line = (kind, html) => `<div class="k-${kind}">${html}</div>`;
const plain = (kind, text) => line(kind, esc(text));
const toolLine = (l) => {
  const [name, ms] = l.text.split(/\s+/);
  return line("tool", `<span class="t">${esc(name)}</span>  <span class="ms">${esc(ms || "")}</span>`);
};
const textLines = (kind, text, max = 99) => String(text).split("\n").slice(0, max).map((s) => plain(kind, s));

// Chapters are numbered by position in the cut, so a clip scene left out
// (one that did not record cleanly) does not leave a gap in the numbering.
const ORDER = T.scenes.filter((x) => !["title", "end"].includes(x.id)).map((x) => x.id);
let CURRENT = "";
function chapter(n, name, t) {
  n = ORDER.indexOf(CURRENT) + 1 || n;
  const p = prog(t, 0.1, 0.9);
  return `<div class="chapter" style="opacity:${p};transform:translateX(${(1 - p) * -24}px)">
    <b>${String(n).padStart(2, "0")}</b><span class="rule" style="width:${p * 40}px"></span>${esc(name)}</div>`;
}

function headline(text, t, at = 0.25) {
  return `<div class="headline" style="${enter(t, at, { dy: 34, over: 1.1, blur: 10 })}">${text}</div>`;
}

// The framed window a real Tally recording plays in. render.mjs composites the
// clip into exactly this rectangle, so the frame and the footage line up.
const CLIP_RECT = { x: 96, y: 150, w: 1216, h: 760 };
function clipFrame(t, label, speed) {
  const p = prog(t, 0.05, 0.8);
  const { x, y, w, h } = CLIP_RECT;
  const blink = Math.floor(t * 1.6) % 2 === 0;
  return `<div class="clipframe" style="left:${x - 12}px;top:${y - 50}px;width:${w + 24}px;height:${h + 62}px;opacity:${p};transform:scale(${lerp(0.96, 1, p)})">
      <div class="clipbar"><span class="rec" style="opacity:${blink ? 1 : 0.35}">●</span> REC · TallyPrime, live on this machine
      <span class="speed">${speed}×</span><span style="margin-left:auto;color:var(--dim);font-weight:500">${esc(label)}</span></div></div>`;
}

// The agent's own checklist, ticking in step with the footage beside it. The
// times are the ones recorded while the clip was being made.
function stepsPanel(t, s) {
  const speed = s.speed || 1;
  const into = (t - 0.6) * speed;  // seconds into the original recording
  const steps = [];
  for (const e of s.events || []) {
    if (e.kind === "say" && !steps.find((x) => x.text === e.text)) steps.push({ text: e.text, start: e.t, done: Infinity });
    if (e.kind === "done") {
      const st = steps.find((x) => x.text === e.text && x.done === Infinity);
      if (st) st.done = e.t;
    }
  }
  const visible = steps.filter((x) => x.start <= into);
  const shown = visible.slice(-9);
  const p = prog(t, 0.4, 0.9);
  return `<div class="steps" style="opacity:${p};transform:translateX(${(1 - p) * 40}px)">
    <div class="cardhead">What the agent is doing</div>
    ${shown.map((x) => {
      const done = into >= x.done;
      const a = prog(into / speed, x.start / speed, 0.35);
      const label = x.text.replace(/: type "(.*)"$/, ' → "$1"').replace(/: (f\d+|enter|ctrl\+a|escape)$/, " → $1");
      return `<div class="step ${done ? "done" : "now"}" style="opacity:${a};transform:translateX(${(1 - a) * 16}px)">
        <span class="mark">${done ? "✓" : `<i class="spin" style="transform:rotate(${(t * 360) % 360}deg)"></i>`}</span>${esc(label)}</div>`;
    }).join("")}
    ${visible.length ? "" : '<div class="step now"><span class="mark"><i class="spin"></i></span>opening Tally…</div>'}
  </div>`;
}

const STATUS = `LIVE · ${D.company} · writes limited to enabled companies`;

// --- scenes ----------------------------------------------------------------------

function logo(t, at) {
  return [..."tallyagent"].map((ch, i) => {
    const p = prog(t, at + i * 0.06, 0.9);
    return `<span style="display:inline-block;opacity:${p};transform:translateY(${(1 - p) * 60}px);filter:blur(${(1 - p) * 10}px);color:${i >= 5 ? "var(--saffron)" : "#fff"}">${ch}</span>`;
  }).join("");
}

const SCENES = {
  title(t) {
    return `<div class="title-xl">${logo(t, 0.35)}</div>
      <div class="sweep" style="width:${prog(t, 1.3, 1.2) * 860}px"></div>
      <div class="tag" style="${enter(t, 2.0, { over: 1.2 })}">An AI staff member for a CA firm &mdash; working inside the Tally books the firm already trusts.</div>`;
  },

  problem(t) {
    const cards = [
      { value: 65, suffix: "%", label: "of practitioners name staff shortage as their top challenge", src: "BCAJ practitioner survey", color: "var(--saffron)" },
      { value: 90, prefix: "60–", suffix: " h", label: "a month reconciling GSTR-2B for a 30-client firm", src: "Practitioner estimate, ICAI AI use-case library", color: "var(--saffron)" },
      { text: "Rule 88D", label: "excess ITC over 2B draws a DRC-01C notice with a 7-day reply window", src: "CGST Rules", color: "var(--risk)" },
    ];
    return chapter(1, "The problem", t) +
      headline("The work grows every month. The people don't.", t) +
      cards.map((c, i) => {
        const at = 1.4 + i * 1.5;
        const big = c.text || `${c.prefix || ""}${count(t, at + 0.2, c.value)}${c.suffix}`;
        return `<div class="card" style="left:${96 + i * 584}px;top:340px;width:548px;${enter(t, at)}">
          <div class="big" style="color:${c.color}">${big}</div>
          <div class="label">${c.label}</div><div class="src">${c.src}</div></div>`;
      }).join("");
  },

  live(t) {
    const steps = [["Tally is not answering", "bad"], ["restarting TallyPrime", ""], ["clearing the licence screen", ""], [`company loaded: ${D.company}`, "ok"]];
    return chapter(2, "Live, not a mock-up", t) +
      headline("Filmed against a real TallyPrime. It was down. The agent brought it back.", t) +
      terminal({
        x: 96, y: 340, w: 1100, h: 400, t, at: 0.9, title: "tallyagent · self-healing connection",
        blocks: [{ at: 1.8, every: 1.25, lines: steps.map(([s, c]) => line("system", `<span class="${c}">${esc(s)}</span>`)) }],
      }) +
      `<div class="card" style="left:1250px;top:340px;width:574px;${enter(t, 6.4)}">
        <div class="big" style="font-size:72px;color:var(--teal)">0 people</div>
        <div class="label">touched the machine to recover it</div></div>`;
  },

  people(t) {
    const users = turn("people", 0), signin = turn("people", 1);
    return chapter(3, "Who is at the keyboard", t) +
      headline("A clerk drafts. A partner decides.", t) +
      terminal({
        x: 96, y: 300, w: 1728, h: 520, t, at: 0.6, title: "tallyagent · TUI", status: STATUS,
        blocks: [
          { at: 1.2, input: users.input },
          { at: 1.8, lines: textLines("system", lineOf("people", 0, "system")) },
          { at: 3.6, input: signin.input },
          { at: 4.6, lines: [plain("system", lineOf("people", 1, "system"))] },
        ],
      });
  },

  sale(t) {
    const tr = turn("sale", 0);
    const agent = money(lineOf("sale", 0, "agent"));
    const [first, ...rest] = agent.split("\n\n");
    const warning = rest.join(" ").slice(0, 230) + (rest.join(" ").length > 230 ? "…" : "");
    return chapter(4, "A sale, in plain words", t) +
      `<div class="note" style="${enter(t, 2.5)}">⏱ real time ${tr.seconds}s · shown faster</div>` +
      terminal({
        x: 96, y: 150, w: 1728, h: 700, t, title: "tallyagent · signed in as Nikhil (clerk)", status: STATUS,
        blocks: [
          { at: 0.5, input: tr.input, cps: 64 },
          { at: 3.0, every: 0.42, lines: tr.lines.filter((l) => l.kind === "tool").map(toolLine) },
          { at: 7.0, lines: [line("agent", md(esc(first)))] },
          { at: 10.4, lines: [line("agent", `<span class="hl">⚠</span> ${md(esc(warning))}`)] },
          { at: 13.4, lines: [plain("approval", lineOf("sale", 0, "approval"))] },
        ],
      });
  },

  approval(t) {
    const refused = turn("sale", 1), signin = turn("sale", 2), approve = turn("sale", 3);
    const ref = (lineOf("sale", 0, "agent").match(/FILM-\d+/) || ["FILM"])[0];
    const pulse = 0.5 + 0.5 * Math.sin(t * 3);
    return chapter(5, "The approval gate", t) +
      terminal({
        x: 96, y: 150, w: 1000, h: 560, t, title: "tallyagent · TUI",
        blocks: [
          { at: 0.5, input: refused.input, cps: 40 },
          { at: 1.2, lines: [plain("error", lineOf("sale", 1, "error").replace("NotPermittedError: ", ""))] },
          { at: 3.2, input: signin.input, cps: 40 },
          { at: 4.2, lines: [plain("system", lineOf("sale", 2, "system"))] },
          { at: 5.4, input: approve.input, cps: 40 },
          { at: 6.0, lines: [plain("approval", lineOf("sale", 3, "approval"))] },
        ],
      }) +
      `<div class="tally" style="left:1140px;top:150px;width:684px;${enter(t, 6.8, { dy: 40 })}">
        <div class="top">TallyPrime <span>Day Book</span></div>
        <div class="sub"><span>${esc(D.company)}</span><span>2-Jun-2026</span></div>
        <table><tr><th>Particulars</th><th>Type</th><th>Ref</th><th class="num">Debit</th></tr>
        <tr class="new" style="box-shadow:inset 4px 0 0 rgba(245,165,36,${0.4 + 0.6 * pulse})"><td>Acme Industries</td><td>Sales</td><td>${esc(ref)}</td><td class="num">7,552.00</td></tr></table>
        <div class="note2">Read back from Tally after the approval. Approver on the audit trail: R. Mehta.</div></div>`;
  },

  lands(t, s) {
    return chapter(6, "Watch it happen in Tally", t) +
      clipFrame(t, "Day Book · before and after the approval", s.speed) + stepsPanel(t, s);
  },

  refusal(t) {
    const tr = turn("refusal", 0);
    return chapter(7, "It asks instead of guessing", t) +
      terminal({
        x: 96, y: 190, w: 1728, h: 640, t, title: "tallyagent · TUI", status: STATUS,
        blocks: [
          { at: 0.5, input: tr.input },
          { at: 1.8, every: 0.5, lines: tr.lines.filter((l) => l.kind === "tool").map(toolLine) },
          { at: 3.8, lines: [line("agent", md(esc(money(lineOf("refusal", 0, "agent")))))] },
        ],
      });
  },

  clients(t) {
    const list = turn("clients", 0), sw = turn("clients", 2), none = turn("clients", 3);
    return chapter(8, "One install, the whole practice", t) +
      headline("Separate books. Separate queues. Separate consent.", t) +
      terminal({
        x: 96, y: 290, w: 1728, h: 550, t, at: 0.6, title: "tallyagent · TUI",
        blocks: [
          { at: 1.2, input: list.input },
          { at: 1.8, lines: textLines("system", lineOf("clients", 0, "system")) },
          { at: 4, input: sw.input },
          { at: 4.8, lines: [plain("system", lineOf("clients", 2, "system"))] },
          { at: 6.6, input: none.input },
          { at: 7.4, lines: [plain("error", lineOf("clients", 3, "system"))] },
        ],
      });
  },

  bills(t) {
    const tr = turn("bills", 0);
    const rows = tr.lines.filter((l) => l.kind === "system").map((l) => plain("system", l.text));
    const acc = (D.files.accuracy_photo || "").split("\n")
      .filter((s) => /^\| (vendor|invoice_no|invoice_date|taxable_value|igst|total) \|/.test(s))
      .map((s) => s.split("|").map((c) => c.trim()).filter(Boolean));
    return chapter(9, "The pile of bills", t) +
      terminal({ x: 96, y: 150, w: 1000, h: 560, t, title: "tallyagent · TUI", blocks: [{ at: 0.5, input: tr.input }, { at: 2, every: 0.9, lines: rows }] }) +
      `<div class="card" style="left:1140px;top:150px;width:684px;${enter(t, 6.2)}">
        <div class="cardhead">Photographed bills — skewed, shadowed, forwarded twice</div>
        ${acc.map(([f, , , pct], i) => {
          const at = 6.8 + i * 0.35;
          const value = Number(String(pct).replace("%", "")) || 0;
          return `<div class="row" style="${enter(t, at, { dy: 12, over: 0.6, blur: 2, scale: 1 })}"><span>${esc(f.replace("_", " "))}</span>
            <span class="bar2"><i style="width:${easeOut((t - at) / 1.2) * value}%"></i></span>
            <span class="amt" style="color:var(--teal)">${count(t, at, value, 1.2)}%</span></div>`;
        }).join("")}
        <div class="src">Every field right on our pile; the unreadable scan was refused, not guessed.</div></div>`;
  },

  invoice(t) {
    t /= 1.75; // paced to the narration: fields, then the draft, then the checks
    const B = S.bill;
    const H = 700, scale = H / B.size[1], W = B.size[0] * scale, X = 96, Y = 170;
    const scanY = clamp((t - 0.8) / 2.6) * H;
    const labels = { vendor: "Supplier", gstin: "GSTIN", invoice_no: "Invoice no", invoice_date: "Date", taxable_value: "Taxable value", igst: "IGST", total: "Total" };
    const boxes = B.fields.map((f, i) => {
      const [x0, y0, x1, y1] = f.box.map((v) => v * scale);
      const at = 0.8 + (y1 / H) * 2.6;
      const p = prog(t, at, 0.45);
      return `<div style="position:absolute;left:${X + x0 - 6}px;top:${Y + y0 - 4}px;width:${x1 - x0 + 12}px;height:${y1 - y0 + 8}px;border:2px solid var(--saffron);border-radius:6px;opacity:${p};box-shadow:0 0 ${18 * p}px rgba(255,153,51,.55);transform:scale(${lerp(1.25, 1, p)})"></div>`;
    }).join("");
    const read = B.fields.map((f, i) => {
      const at = 0.8 + ((f.box[3] * scale) / H) * 2.6 + 0.2;
      return `<div class="row" style="padding:5px 0;font-size:22px;${enter(t, at, { dy: 10, over: 0.5, blur: 2, scale: 1 })}"><span>${labels[f.field] || f.field}</span><span class="amt" style="color:var(--ink)">${esc(typed(f.value, t, at + 0.1, 40).text)}</span></div>`;
    }).join("");
    const lines = B.lines.map((l, i) => {
      const at = 5.6 + i * 0.55;
      return `<div class="row" style="padding:8px 0;font-size:23px;${enter(t, at, { dy: 12, over: 0.6, blur: 2, scale: 1 })}"><span><b style="color:${l.side === "Dr" ? "var(--teal)" : "var(--saffron)"}">${l.side}</b>&nbsp; ${esc(l.ledger)}</span><span class="amt" style="color:var(--ink)">${esc(l.amount)}</span></div>`;
    }).join("");
    const dr = B.lines.filter((l) => l.side === "Dr").reduce((a, l) => a + Number(l.amount.replace(/,/g, "")), 0);
    const balanced = prog(t, 7.6, 0.8);
    const checks = B.checks.slice(0, 5).map((c, i) => `<span class="pill" style="${enter(t, 8.4 + i * 0.25, { dy: 6, over: 0.4, blur: 0, scale: 1 })}">${c.passed ? "✓" : "✗"} ${esc(c.rule.replace(/_/g, " "))}</span>`).join(" ");
    return chapter(10, "A bill becomes a voucher", t) +
      `<div style="position:absolute;left:${X}px;top:${Y}px;width:${W}px;height:${H}px;border-radius:10px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.45);${enter(t, 0.1)}">
        <img src="${B.image}" style="width:100%;height:100%;display:block">
        <div style="position:absolute;left:0;right:0;top:${scanY}px;height:3px;background:var(--saffron);box-shadow:0 0 24px 6px rgba(255,153,51,.6);opacity:${t > 0.8 && t < 3.6 ? 1 : 0}"></div></div>` +
      boxes +
      `<div class="card" style="left:${X + W + 60}px;top:170px;width:${1824 - X - W - 60}px;${enter(t, 0.9)}">
        <div class="cardhead">Read from the scan</div>${read}</div>` +
      `<div class="card" style="left:${X + W + 60}px;top:572px;width:${1824 - X - W - 60}px;${enter(t, 5.2)}">
        <div class="cardhead">Drafted purchase voucher · ${esc(B.ticket)}</div>${lines}
        <div class="row" style="padding:8px 0;font-size:23px;opacity:${balanced};border-top:1px solid var(--line)"><span style="color:var(--teal)">Dr ${count(t, 7.6, dr, 1, 2)} = Cr ${count(t, 7.6, dr, 1, 2)}</span><span class="amt" style="color:var(--teal)">balanced ✓</span></div>
        <div style="margin-top:10px">${checks}</div></div>`;
  },

  matching(t) {
    t /= 1.9; // paced to the narration: bank first, then 2B
    const R = S.reco, bank = R.bank, gst = R.gstr2b;
    const n = (s) => Number(String(s).replace(/,/g, ""));
    const fmt = (v) => Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: 2 });
    const rowY = (i) => 236 + i * 74;
    const matchOf = (r) => bank.matched.find((m) => n(m.amount) === n(r.amount) && m.statement_date === r.date.split("/").reverse().join("-"));
    const stmt = bank.statement.map((r, i) => {
      const v = n(r.amount);
      return `<div class="row" style="position:absolute;left:96px;top:${rowY(i)}px;width:640px;${enter(t, 0.6 + i * 0.25, { dy: 10, over: 0.5, blur: 2, scale: 1 })}">
        <span style="font-size:22px">${esc(r.date)} · ${esc(r.narration)}</span><span class="amt" style="color:${v > 0 ? "var(--teal)" : "var(--saffron)"}">${v > 0 ? "Cr" : "Dr"} ${fmt(v)}</span></div>`;
    }).join("");
    let k = 0;
    const links = [], books = [], props = [];
    bank.statement.forEach((r, i) => {
      const m = matchOf(r);
      const at = 2.4 + i * 0.9;
      if (m) {
        const by = rowY(k++);
        const p = prog(t, at, 0.7);
        links.push(`<path d="M736 ${rowY(i) + 26} C 900 ${rowY(i) + 26}, 960 ${by + 26}, 1124 ${by + 26}" stroke="var(--teal)" stroke-width="3" fill="none" stroke-dasharray="520" stroke-dashoffset="${520 * (1 - p)}"/>`);
        books.push(`<div class="row" style="position:absolute;left:1124px;top:${by}px;width:700px;${enter(t, at + 0.4, { dy: 8, over: 0.5, blur: 2, scale: 1 })}">
          <span style="font-size:22px">Voucher ${esc(m.voucher_number)} · ${esc(m.statement_date)}</span><span class="amt" style="color:var(--teal)">${fmt(n(m.amount))} ✓ matched</span></div>`);
      } else {
        const p = bank.proposals.find((x) => x.narration === r.narration) || {};
        const receipt = p.tool === "create_receipt";
        const party = p.suggested_party || "ledger for a person to pick";
        const dr = receipt ? p.bank_ledger : party, cr = receipt ? party : p.bank_ledger;
        props.push(`<div class="row" style="position:absolute;left:1124px;top:${rowY(bank.matched.length + props.length) + 20}px;width:700px;${enter(t, at + 0.3, { dy: 8, over: 0.5, blur: 2, scale: 1 })}">
          <span style="font-size:21px"><b style="color:var(--saffron)">no voucher</b> → ${receipt ? "Receipt" : "Payment"}: Dr ${esc(dr)} / Cr ${esc(cr)}</span><span class="amt" style="color:var(--ink)">${esc(fmt(n(p.amount || 0)))}</span></div>`);
      }
    });
    const tone = { matched: "var(--teal)", value_mismatch: "var(--saffron)", missing_in_2b: "var(--risk)", missing_in_books: "var(--saffron)" };
    const words = { matched: "matched", value_mismatch: "value differs", missing_in_2b: "supplier hasn't filed", missing_in_books: "not in books" };
    const g = gst.rows.map((r, i) => `<div class="pill" style="display:inline-block;margin:6px 8px 0 0;border-color:${tone[r.status]};${enter(t, 7.4 + i * 0.3, { dy: 8, over: 0.5, blur: 2, scale: 1 })}">
      ${esc(r.invoice_no)} · <b style="color:${tone[r.status]}">${words[r.status] || r.status}</b>${Number(r.itc_at_risk) ? ` · ITC Rs ${fmt(n(r.itc_at_risk))} held` : ""}</div>`).join("");
    return chapter(11, "Debits and credits, matched", t) +
      `<div class="cardhead" style="position:absolute;left:96px;top:180px;${enter(t, 0.3)}">Bank statement · June</div>` +
      `<div class="cardhead" style="position:absolute;left:1124px;top:180px;${enter(t, 0.3)}">Books in Tally · ${esc("Bank - HDFC 1234")}</div>` +
      stmt + `<svg style="position:absolute;left:0;top:0" width="1920" height="1080">${links.join("")}</svg>` + books.join("") + props.join("") +
      `<div class="card" style="left:96px;top:620px;width:1728px;${enter(t, 7)}">
        <div class="cardhead">GSTR-2B against the purchase register — on supplier GSTIN and invoice number</div>${g}
        <div class="src" style="${enter(t, 9.6)}">${esc(gst.message)}</div></div>`;
  },

  firm(t) {
    const inbox = lineOf("firm", 1, "system").split("\n").filter((s) => s.includes("#"));
    const parse = (s) => {
      const m = s.match(/#(\d+)\s+\[(\w+)\s*\]\s+(\S+)\s+(.*?)(?:\s{2,}Rs ([\d,.]+))?(?:\s{2,}due (\S+))?\s*$/);
      return m ? { kind: m[2], client: m[3], title: m[4], amt: m[5] } : null;
    };
    const items = inbox.map(parse).filter(Boolean).slice(0, 7);
    return chapter(10, "Every client, every morning", t) +
      `<div class="card" style="left:96px;top:150px;width:560px;${enter(t, 0.3)}">
        <div class="cardhead">Morning run · 07:30</div>
        ${D.run.clients.map((c, i) => `<div class="row" style="${enter(t, 0.9 + i * 0.4, { dy: 10, over: 0.6, blur: 2, scale: 1 })}"><span class="who">${esc(c.slug)}</span>
          <span style="font-size:20px;color:${c.healthy ? "var(--ink)" : "var(--risk)"}">${c.healthy ? `${c.done} done · ${c.queued} waiting · ${c.exceptions} for a person` : "Tally not loaded — reported, run continued"}</span></div>`).join("")}
        </div>
      <div class="card" style="left:700px;top:150px;width:1124px;${enter(t, 1.8)}">
        <div class="cardhead">Firm inbox — money at risk first</div>
        ${items.map((it, i) => {
          const at = 2.6 + i * 0.55;
          const amount = it.amt ? Number(it.amt.replace(/,/g, "")) : 0;
          return `<div class="row" style="${enter(t, at, { dy: 12, over: 0.6, blur: 2, scale: 1 })}">
          <span class="pill ${it.kind === "exception" ? "ex" : it.kind === "queued" ? "q" : "d"}">${it.kind === "exception" ? "person" : it.kind === "queued" ? "waiting" : "done"}</span>
          <span style="font-size:22px">${esc(it.title)}</span>
          ${amount ? `<span class="amt">Rs ${count(t, at + 0.2, amount, 1.4, 2)}</span>` : ""}</div>`;
        }).join("")}
      </div>`;
  },

  followup(t) {
    const text = D.files.followup || "";
    const shown = typed(text, t, 0.7, 150);
    return chapter(11, "The follow-up is already written", t) +
      `<div class="letter" style="left:300px;top:140px;width:1320px;height:700px;${enter(t, 0.2, { dy: 50, over: 1.1 })}">${esc(shown.text)}${shown.done ? "" : caret(t, true)}</div>`;
  },

  autonomy(t) {
    const N = 20;
    const filled = Math.floor(clamp((t - 1.2) / 7) * N);
    const broken = t > 11.6;
    const status = lineOf("firm", 2, "system").split("\n");
    const nt = status.find((x) => /no-touch/.test(x)) || "";
    const pct = Number((nt.match(/(\d+)% no-touch/) || [])[1] || 0);
    return chapter(12, "Earned autonomy", t) +
      headline("Throughput that has to be earned — and is lost at once.", t) +
      `<div class="card" style="left:96px;top:300px;width:1000px;${enter(t, 0.7)}">
        <div class="cardhead">create_receipt · clean approvals in a row</div>
        <div class="dots">${Array.from({ length: N }, (_, i) => {
          const on = !broken && i < filled;
          const pop = on ? prog(t, 1.2 + (i / N) * 7, 0.35) : 0;
          return `<i class="${broken && i === 0 ? "bad" : on ? "on" : ""}" style="transform:scale(${on ? lerp(0.6, 1, pop) : 1})"></i>`;
        }).join("")}</div>
        <div class="state" style="color:${broken ? "var(--risk)" : filled >= N ? "var(--teal)" : "var(--ink)"}">
          ${broken ? "One correction → back to waiting for a person" : filled >= N ? "Trusted: posts by itself, up to the largest amount a person approved" : `Gated — ${filled} of ${N}`}</div>
        <div class="src">A partner switches it on per client, with a streak and a rupee ceiling. The system never grants itself autonomy.</div></div>
      <div class="card" style="left:1140px;top:300px;width:684px;${enter(t, 3)}">
        <div class="cardhead">/autonomy — live, ${esc(D.company)}</div>
        ${status.slice(1).filter((x) => /streak/.test(x)).map((x, i) => {
          const m = x.trim().match(/^(\S+)\s+streak\s+(\d+)/) || [];
          return `<div class="row" style="font-size:21px;${enter(t, 3.4 + i * 0.3, { dy: 10, over: 0.6, blur: 2, scale: 1 })}"><span style="font-family:var(--mono)">${esc(m[1] || "")}</span>
            <span style="margin-left:auto;color:var(--dim)">streak ${esc(m[2] || "0")} · waiting for a grant</span></div>`;
        }).join("")}
        <div style="margin-top:22px;display:flex;align-items:baseline;gap:16px">
          <div class="big" style="font-size:72px;color:var(--teal)">${count(t, 4.4, pct)}%</div>
          <div class="label" style="margin:0">no-touch rate today<br><span style="font-size:18px">${esc(nt.trim().replace(/^\d+% no-touch: /, ""))}</span></div></div>
      </div>`;
  },

  keyed(t, s) {
    return chapter(13, "It works Tally's own screens", t) +
      clipFrame(t, "Payment · keyed field by field", s.speed) + stepsPanel(t, s);
  },

  billwise(t, s) {
    return chapter(14, "Even when Tally interrupts", t) +
      clipFrame(t, "Receipt · Tally asks for bill-wise details", s.speed) + stepsPanel(t, s);
  },

  close(t) {
    const me = turn("close", 0), g1 = turn("close", 1);
    return chapter(15, "The first of the month", t) +
      terminal({
        x: 96, y: 150, w: 1728, h: 690, t, title: "tallyagent · TUI",
        blocks: [
          { at: 0.5, input: me.input, cps: 72 },
          { at: 2.2, lines: [plain("system", lineOf("close", 0, "system"))] },
          { at: 3, every: 0.62, lines: textLines("system", (me.lines[1] || {}).text || "", 12).map((l) => l.replace(/(\(\d[\d.]*\))/g, '<span class="bad">$1</span>')) },
          { at: 9.2, input: g1.input },
          { at: 10, lines: [plain("approval", lineOf("close", 1, "system"))] },
        ],
      });
  },

  audit(t) {
    const rows = D.audit.slice(-6);
    return chapter(16, "Who did what, provably", t) +
      `<div class="chain">
        ${rows.map((r, i) => {
          const at = 0.6 + i * 0.55;
          const linkP = prog(t, at - 0.2, 0.5);
          return `${i ? `<div class="link"><i style="width:${linkP * 100}%"></i></div>` : ""}<div class="block" style="${enter(t, at, { dy: 20, over: 0.7 })}">
            <b>${esc(r.event.replace(/_/g, " "))}</b>${esc(r.actor.slice(0, 26))}<div class="h">#${r.seq}</div></div>`;
        }).join("")}
      </div>
      <div class="card" style="left:96px;top:560px;width:1728px;${enter(t, 4.6)}">
        <div class="big" style="font-size:72px;color:var(--teal)">${D.audit_ok ? "✓ Chain intact" : "Chain broken"}</div>
        <div class="label">${count(t, 4.8, D.audit_count)} records verified — every hash recomputed. Change one byte anywhere and it says where.</div></div>`;
  },

  trust(t) {
    const items = [
      ["Consent pinned to the books", "Writes go only to companies a partner enabled — bound to Tally's own company GUID."],
      ["Names, not 'web'", "Clerk and partner roles; every approval carries a person checked by PIN."],
      ["Runs in your office", "A local model option: no client data leaves the building. Every byte logged."],
      ["Edit log checked", "Company clients without Tally's edit log are flagged — Companies Act Rule 3(1)."],
    ];
    return chapter(17, "Why a CA can trust it", t) +
      items.map(([h, b], i) => `<div class="card" style="left:${96 + (i % 2) * 884}px;top:${200 + Math.floor(i / 2) * 340}px;width:844px;height:300px;${enter(t, 0.4 + i * 0.9)}">
        <div class="trusthead">${h}</div><div class="label" style="font-size:27px">${b}</div></div>`).join("");
  },

  end(t) {
    return `<div class="title-xl" style="top:300px">${logo(t, 0.2)}</div>
      <div class="sweep" style="top:470px;width:${prog(t, 1, 1.1) * 860}px"></div>
      <div class="tag" style="top:510px;${enter(t, 1.4)}">Your Tally. Two clients. Two weeks.</div>`;
  },
};

// --- the frame -----------------------------------------------------------------

const SPELLED = [
  [/G S T R two B/g, "GSTR-2B"], [/G S T R one/g, "GSTR-1"], [/\bG S T\b/g, "GST"],
  [/\bA I\b/g, "AI"], [/\bC A\b/g, "CA"], [/\bX M L\b/g, "XML"], [/Ctrl A/g, "Ctrl+A"],
  [/hands off/g, "hands-off"], [/hash chained/g, "hash-chained"], [/follow up/g, "follow-up"],
  [/bill wise/g, "bill-wise"], [/Sixty five percent/g, "65%"],
];
const written = (say) => SPELLED.reduce((text, [from, to]) => text.replace(from, to), say);

// Words fade in as they are spoken, rather than flipping on.
function captionHtml(s, local) {
  const words = written(s.say).split(/\s+/);
  const into = (local - (s.voice_at - s.start)) / s.voice_seconds;
  if (into < -0.04 || local > s.duration - 0.25) return "";
  const pos = clamp(into) * words.length;
  return words.map((w, i) => `<span style="opacity:${lerp(0.32, 1, clamp(pos - i + 0.6))}">${esc(w)}</span>`).join(" ");
}

function background(time) {
  const a = time * 0.05, b = time * 0.037;
  return `<div class="orb" style="left:${1300 + Math.sin(a) * 220}px;top:${-260 + Math.cos(b) * 120}px;background:radial-gradient(closest-side,#1d3366,transparent)"></div>
    <div class="orb" style="left:${-300 + Math.cos(a * 0.8) * 180}px;top:${620 + Math.sin(b * 1.3) * 140}px;background:radial-gradient(closest-side,#3a2408,transparent)"></div>
    <div class="orb small" style="left:${820 + Math.sin(b * 1.7) * 300}px;top:${380 + Math.cos(a * 1.2) * 160}px;background:radial-gradient(closest-side,rgba(34,195,166,.10),transparent)"></div>`;
}

// Scenes overlap for TRANSITION seconds: the outgoing one recedes and blurs
// while the incoming one rises in - a dissolve, not a cut.
const TRANSITION = 0.7;

function layer(s, local, alpha, out) {
  CURRENT = s.id;
  const body = (SCENES[s.id] || (() => ""))(Math.max(0, local), s);
  const scale = out ? lerp(1, 0.97, 1 - alpha) : lerp(1.015, 1, alpha);
  return `<div class="layer" style="opacity:${alpha};transform:scale(${scale});filter:blur(${(1 - alpha) * 10}px)">${body}</div>`;
}

window.render = function render(time) {
  const scenes = T.scenes;
  let i = scenes.findIndex((s) => time < s.start + s.duration);
  if (i < 0) i = scenes.length - 1;
  const s = scenes[i];
  const local = time - s.start;
  let html = background(time);

  const toEnd = s.duration - local;
  const next = scenes[i + 1];
  if (next && toEnd < TRANSITION) {
    const p = easeInOut(1 - toEnd / TRANSITION);
    html += layer(s, local, 1 - p, true) + layer(next, local - s.duration, p, false);
  } else {
    html += layer(s, local, i === 0 ? easeInOut(local / 0.8) : 1, false);
  }

  const total = T.total;
  html += `<div class="brand">tally<i>agent</i></div>
    <div class="caption">${captionHtml(s, local)}</div>
    <div class="progress" style="width:${(time / total) * 100}%"></div>
    <div class="ticks">${scenes.map((x) => `<i style="left:${(x.start / total) * 100}%;opacity:${time >= x.start ? 0.9 : 0.25}"></i>`).join("")}</div>`;
  stage.innerHTML = html;
};

window.TOTAL = T.total;
window.CLIP_RECT = CLIP_RECT;
const q = new URLSearchParams(location.search).get("t");
render(q ? Number(q) : 0);
