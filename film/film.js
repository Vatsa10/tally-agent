// The tallyagent film, drawn as a pure function of time.
//
// Every frame is render(t): nothing animates on its own, so the renderer can
// step through the film one frame at a time and every frame is exact. Every
// line a terminal "prints" here is a line the product printed, captured live
// by scripts/film_capture.py into CAPTURE; scene lengths come from the
// narration in TIMING.

const D = window.CAPTURE;
const T = window.TIMING;
const stage = document.getElementById("stage");

// --- helpers ------------------------------------------------------------------

const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const ease = (x) => 1 - Math.pow(1 - clamp(x), 3);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const appear = (t, at, over = 0.5) => ease((t - at) / over);
const style = (t, at, dy = 24, over = 0.6) => {
  const a = appear(t, at, over);
  return `opacity:${a};transform:translateY(${(1 - a) * dy}px)`;
};

function typed(text, t, at, cps = 34) {
  const n = Math.floor(clamp((t - at) * cps, 0, text.length));
  const done = n >= text.length;
  return { text: text.slice(0, n), done, caret: !done && t >= at };
}

function scene(id) { return D.scenes[id] || []; }
function turn(id, i) { return scene(id)[i] || { input: "", lines: [] }; }
function lineOf(id, i, kind) { return (turn(id, i).lines.find((l) => l.kind === kind) || {}).text || ""; }

function money(s) { return s.replace(/₹/g, "Rs "); }
const md = (html) => html.replace(/\*\*(.+?)\*\*/g, '<span class="hl">$1</span>');

// A terminal that types its input, then reveals the output a line at a time.
function terminal({ x, y, w, h, title, status = "", blocks, t }) {
  let body = "";
  for (const b of blocks) {
    if (t < b.at) break;
    if (b.input !== undefined) {
      const ty = typed(b.input, t, b.at, b.cps || 34);
      body += `<div class="k-user">${esc(ty.text)}${ty.caret ? '<span class="caret"></span>' : ""}</div>`;
      if (!ty.done) break;
      continue;
    }
    const lines = b.lines || [];
    const every = b.every ?? 0.35;
    const shown = Math.floor((t - b.at) / every) + 1;
    lines.slice(0, Math.max(0, shown)).forEach((l) => { body += l; });
  }
  return `<div class="term" style="left:${x}px;top:${y}px;width:${w}px;height:${h}px">
    <div class="bar"><span class="dot"></span><span class="dot"></span><span class="dot"></span>
    <span class="title">${esc(title)}</span><span class="status">${esc(status)}</span></div>
    <div class="body">${body}</div></div>`;
}

const line = (kind, html) => `<div class="k-${kind}">${html}</div>`;
const plain = (kind, text) => line(kind, esc(text));

function toolLine(l) {
  const parts = l.text.split(/\s+/);
  return line("tool", `<span class="t">${esc(parts[0])}</span>  ${esc(parts[1] || "")}`);
}

function textLines(kind, text, max = 99) {
  return String(text).split("\n").slice(0, max).map((s) => plain(kind, s));
}

function chapter(n, name) {
  return `<div class="chapter"><b>${String(n).padStart(2, "0")}</b>${esc(name)}</div>`;
}

function headline(text, t, at = 0.2) {
  return `<div class="headline" style="${style(t, at)}">${text}</div>`;
}

const STATUS = `LIVE · ${D.company} · writes limited to enabled companies`;

// --- scenes ---------------------------------------------------------------------

const SCENES = {
  title(t) {
    return `<div class="title-xl" style="${style(t, 0.3, 40, 1)}">tally<i>agent</i></div>
      <div class="tag" style="${style(t, 1.2, 30, 1)}">An AI staff member for a CA firm &mdash;
      working inside the Tally books the firm already trusts.</div>`;
  },

  problem(t) {
    const cards = [
      ["65%", "of practitioners name staff shortage as their top challenge", "BCAJ practitioner survey"],
      ["60–90 h", "a month reconciling GSTR-2B for a 30-client firm", "Practitioner estimate, ICAI AI use-case library"],
      ["Rule 88D", "excess ITC over 2B draws a DRC-01C notice with a 7-day reply window", "CGST Rules"],
    ];
    return chapter(1, "The problem") +
      headline("The work grows every month. The people don't.", t) +
      cards.map(([big, label, src], i) => `<div class="card" style="left:${96 + i * 584}px;top:330px;width:548px;${style(t, 1.2 + i * 1.4)}">
        <div class="big" style="color:${i === 2 ? "var(--risk)" : "var(--saffron)"}">${big}</div>
        <div class="label">${label}</div><div class="src">${src}</div></div>`).join("");
  },

  live(t) {
    const steps = [
      ["Tally is not answering", "bad"],
      ["restarting TallyPrime", ""],
      ["clearing the licence screen", ""],
      ["company loaded: " + D.company, "ok"],
    ];
    return chapter(2, "Live, not a mock-up") +
      headline("Filmed against a real TallyPrime. It was down. The agent brought it back.", t) +
      terminal({
        x: 96, y: 340, w: 1100, h: 400, t, title: "tallyagent · self-healing connection", status: "",
        blocks: [{ at: 1.5, every: 1.2, lines: steps.map(([s, c]) => line("system", `<span class="${c}">${esc(s)}</span>`)) }],
      }) +
      `<div class="card" style="left:1250px;top:340px;width:574px;${style(t, 6)}">
        <div class="big" style="font-size:64px;color:var(--teal)">0 people</div>
        <div class="label">touched the machine to recover it</div></div>`;
  },

  people(t) {
    const users = turn("people", 0), signin = turn("people", 1);
    return chapter(3, "Who is at the keyboard") +
      headline("A clerk drafts. A partner decides.", t) +
      terminal({
        x: 96, y: 290, w: 1728, h: 540, t, title: "tallyagent · TUI", status: STATUS,
        blocks: [
          { at: 1, input: users.input },
          { at: 1.6, lines: textLines("system", lineOf("people", 0, "system")) },
          { at: 3.6, input: signin.input },
          { at: 4.6, lines: [plain("system", lineOf("people", 1, "system"))] },
        ],
      });
  },

  sale(t) {
    const tr = turn("sale", 0);
    const tools = tr.lines.filter((l) => l.kind === "tool").map(toolLine);
    const agent = money(lineOf("sale", 0, "agent"));
    const [first, ...rest] = agent.split("\n\n");
    const warning = rest.join(" ").slice(0, 230) + (rest.join(" ").length > 230 ? "…" : "");
    return chapter(4, "A sale, in plain words") +
      terminal({
        x: 96, y: 150, w: 1728, h: 690, t, title: "tallyagent · signed in as Nikhil (clerk)", status: STATUS,
        blocks: [
          { at: 0.4, input: tr.input, cps: 60 },
          { at: 3.0, every: 0.45, lines: tools },
          { at: 7.0, lines: [line("agent", md(esc(first)))] },
          { at: 10.5, lines: [line("agent", `<span class="hl">⚠</span> ${esc(warning)}`)] },
          { at: 13.5, lines: [plain("approval", lineOf("sale", 0, "approval"))] },
        ],
      }) +
      `<div style="position:absolute;right:96px;top:112px;font-size:17px;color:var(--faint);font-family:var(--mono)">⏱ real time ${tr.seconds}s · shown faster</div>`;
  },

  approval(t) {
    const refused = turn("sale", 1), signin = turn("sale", 2), approve = turn("sale", 3);
    const posted = lineOf("sale", 3, "approval");
    const ref = (lineOf("sale", 0, "agent").match(/FILM-\d+/) || ["FILM"])[0];
    return chapter(5, "The approval gate") +
      terminal({
        x: 96, y: 150, w: 1000, h: 560, t, title: "tallyagent · TUI", status: "",
        blocks: [
          { at: 0.4, input: refused.input, cps: 40 },
          { at: 1.2, lines: [plain("error", lineOf("sale", 1, "error").replace("NotPermittedError: ", ""))] },
          { at: 3.2, input: signin.input, cps: 40 },
          { at: 4.2, lines: [plain("system", lineOf("sale", 2, "system"))] },
          { at: 5.4, input: approve.input, cps: 40 },
          { at: 6.0, lines: [plain("approval", posted)] },
        ],
      }) +
      `<div class="tally" style="left:1140px;top:150px;width:684px;${style(t, 7.0, 30, 0.8)}">
        <div class="top">TallyPrime <span>Day Book</span></div>
        <div class="sub"><span>${esc(D.company)}</span><span>2-Jun-2026</span></div>
        <table><tr><th>Particulars</th><th>Type</th><th>Ref</th><th class="num">Debit</th></tr>
        <tr class="new"><td>Acme Industries</td><td>Sales</td><td>${esc(ref)}</td><td class="num">7,552.00</td></tr></table>
        <div class="note">Read back from Tally over XML after the approval. Approver on the audit trail: R. Mehta.</div></div>`;
  },

  refusal(t) {
    const tr = turn("refusal", 0);
    return chapter(6, "It asks instead of guessing") +
      terminal({
        x: 96, y: 190, w: 1728, h: 640, t, title: "tallyagent · TUI", status: STATUS,
        blocks: [
          { at: 0.4, input: tr.input },
          { at: 1.8, every: 0.5, lines: tr.lines.filter((l) => l.kind === "tool").map(toolLine) },
          { at: 3.8, lines: [line("agent", md(esc(money(lineOf("refusal", 0, "agent")))))] },
        ],
      });
  },

  clients(t) {
    const list = turn("clients", 0), sw = turn("clients", 2), none = turn("clients", 3);
    return chapter(7, "One install, the whole practice") +
      headline("Separate books. Separate queues. Separate consent.", t) +
      terminal({
        x: 96, y: 290, w: 1728, h: 550, t, title: "tallyagent · TUI", status: "",
        blocks: [
          { at: 1, input: list.input },
          { at: 1.6, lines: textLines("system", lineOf("clients", 0, "system")) },
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
    const acc = (D.files.accuracy_photo || "").split("\n").filter((s) => /^\| (vendor|invoice_no|invoice_date|taxable_value|igst|total) \|/.test(s))
      .map((s) => s.split("|").map((c) => c.trim()).filter(Boolean));
    return chapter(8, "The pile of bills") +
      terminal({
        x: 96, y: 150, w: 1000, h: 560, t, title: "tallyagent · TUI", status: "",
        blocks: [{ at: 0.4, input: tr.input }, { at: 2, every: 0.9, lines: rows }],
      }) +
      `<div class="card" style="left:1140px;top:150px;width:684px;${style(t, 6.5)}">
        <div style="font-size:22px;color:var(--dim);margin-bottom:10px">Photographed bills — skewed, shadowed, forwarded twice</div>
        ${acc.map(([f, read, right, pct]) => `<div class="row"><span>${esc(f.replace("_", " "))}</span><span class="amt" style="color:var(--teal)">${esc(pct)}</span></div>`).join("")}
        <div class="src">Every field right on our pile; the unreadable scan was refused, not guessed. reports/bill_accuracy_photo.md</div></div>`;
  },

  firm(t) {
    const inbox = lineOf("firm", 1, "system").split("\n").filter((s) => s.includes("#"));
    const parse = (s) => {
      const m = s.match(/#(\d+)\s+\[(\w+)\s*\]\s+(\S+)\s+(.*?)(?:\s{2,}Rs ([\d,.]+))?(?:\s{2,}due (\S+))?\s*$/);
      return m ? { kind: m[2], client: m[3], title: m[4], amt: m[5], due: m[6] } : null;
    };
    const items = inbox.map(parse).filter(Boolean).slice(0, 7);
    const runs = D.run.clients;
    return chapter(9, "Every client, every morning") +
      `<div class="card" style="left:96px;top:150px;width:560px;${style(t, 0.3)}">
        <div style="font-size:22px;color:var(--dim)">Morning run · 07:30</div>
        ${runs.map((c) => `<div class="row"><span class="who">${esc(c.slug)}</span>
          <span style="font-size:20px;color:${c.healthy ? "var(--ink)" : "var(--risk)"}">${c.healthy ? `${c.done} done · ${c.queued} waiting · ${c.exceptions} for a person` : "Tally not loaded — reported, run continued"}</span></div>`).join("")}
        </div>
      <div class="card" style="left:700px;top:150px;width:1124px;${style(t, 2)}">
        <div style="font-size:22px;color:var(--dim);margin-bottom:6px">Firm inbox — money at risk first</div>
        ${items.map((it, i) => `<div class="row" style="${style(t, 3 + i * 0.7, 12, 0.4)}">
          <span class="pill ${it.kind === "exception" ? "ex" : it.kind === "queued" ? "q" : "d"}">${it.kind === "exception" ? "person" : it.kind === "queued" ? "waiting" : "done"}</span>
          <span style="font-size:22px">${esc(it.title)}</span>
          ${it.amt ? `<span class="amt">Rs ${esc(it.amt)}</span>` : ""}</div>`).join("")}
      </div>`;
  },

  followup(t) {
    const text = D.files.followup || "";
    const shown = typed(text, t, 0.6, 140);
    return chapter(10, "The follow-up is already written") +
      `<div class="letter" style="left:300px;top:140px;width:1320px;height:700px;${style(t, 0.2)}">${esc(shown.text)}</div>`;
  },

  autonomy(t) {
    const N = 20;
    const filled = Math.floor(clamp((t - 1) / 7) * N);
    const broken = t > 11.5;
    const status = lineOf("firm", 2, "system").split("\n");
    return chapter(11, "Earned autonomy") +
      headline("Throughput that has to be earned — and is lost at once.", t) +
      `<div class="card" style="left:96px;top:300px;width:1000px;${style(t, 0.6)}">
        <div style="font-size:24px;color:var(--dim);margin-bottom:22px">create_receipt · clean approvals in a row</div>
        <div class="dots">${Array.from({ length: N }, (_, i) => `<i class="${broken && i === 0 ? "bad" : (!broken && i < filled) ? "on" : ""}"></i>`).join("")}</div>
        <div style="margin-top:30px;font-size:30px;font-weight:650;color:${broken ? "var(--risk)" : filled >= N ? "var(--teal)" : "var(--ink)"}">
          ${broken ? "One correction → back to waiting for a person" : filled >= N ? "Trusted: posts by itself, up to the largest amount a person approved" : `Gated — ${filled} of ${N}`}</div>
        <div class="src">A partner switches it on per client, with a streak and a rupee ceiling. The system never grants itself autonomy.</div></div>
      <div class="card" style="left:1140px;top:300px;width:684px;${style(t, 3)}">
        <div style="font-size:22px;color:var(--dim);margin-bottom:6px">/autonomy — live, ${esc(D.company)}</div>
        ${status.slice(1).filter((x) => /streak/.test(x)).map((x) => {
          const m = x.trim().match(/^(\S+)\s+streak\s+(\d+)/) || [];
          return `<div class="row" style="font-size:21px"><span style="font-family:var(--mono)">${esc(m[1] || "")}</span>
            <span style="margin-left:auto;color:var(--dim)">streak ${esc(m[2] || "0")} · waiting for a grant</span></div>`;
        }).join("")}
        ${(() => {
          const nt = status.find((x) => /no-touch/.test(x)) || "";
          const pct = (nt.match(/(\d+)% no-touch/) || [])[1] || "0";
          return `<div style="margin-top:22px;display:flex;align-items:baseline;gap:16px">
            <div class="big" style="font-size:72px;color:var(--teal)">${pct}%</div>
            <div class="label" style="margin:0">no-touch rate today<br><span style="font-size:18px">${esc(nt.trim().replace(/^\d+% no-touch: /, ""))}</span></div></div>`;
        })()}
      </div>`;
  },

  close(t) {
    const me = turn("close", 0), g1 = turn("close", 1);
    return chapter(12, "The first of the month") +
      terminal({
        x: 96, y: 150, w: 1728, h: 690, t, title: "tallyagent · TUI", status: "",
        blocks: [
          { at: 0.4, input: me.input, cps: 70 },
          { at: 2.2, lines: [plain("system", lineOf("close", 0, "system"))] },
          { at: 3, every: 0.7, lines: textLines("system", (me.lines[1] || {}).text || "", 12).map((l) => l.replace(/(\(\d[\d.]*\))/g, '<span class="bad">$1</span>')) },
          { at: 9.2, input: g1.input },
          { at: 10, lines: [plain("approval", lineOf("close", 1, "system"))] },
        ],
      });
  },

  audit(t) {
    const rows = D.audit.slice(-6);
    return chapter(13, "Who did what, provably") +
      `<div style="position:absolute;left:96px;top:240px;display:flex;gap:0;${style(t, 0.4)}">
        ${rows.map((r, i) => `${i ? '<div class="link"></div>' : ""}<div class="block" style="${style(t, 0.6 + i * 0.6, 16, 0.4)}">
          <b>${esc(r.event.replace(/_/g, " "))}</b>${esc(r.actor.slice(0, 26))}<div class="h">#${r.seq}</div></div>`).join("")}
      </div>
      <div class="card" style="left:96px;top:560px;width:1728px;${style(t, 5)}">
        <div class="big" style="font-size:72px;color:var(--teal)">${D.audit_ok ? "Chain intact" : "Chain broken"}</div>
        <div class="label">${D.audit_count} records verified — every hash recomputed. Change one byte anywhere and it says where.</div></div>`;
  },

  trust(t) {
    const items = [
      ["Consent pinned to the books", "Writes go only to companies a partner enabled — bound to Tally's own company GUID."],
      ["Names, not 'web'", "Clerk and partner roles; every approval carries a person checked by PIN."],
      ["Runs in your office", "A local model option: no client data leaves the building. Every byte logged."],
      ["Edit log checked", "Company clients without Tally's edit log are flagged — Companies Act Rule 3(1)."],
    ];
    return chapter(14, "Why a CA can trust it") +
      items.map(([h, b], i) => `<div class="card" style="left:${96 + (i % 2) * 884}px;top:${200 + Math.floor(i / 2) * 340}px;width:844px;height:300px;${style(t, 0.4 + i * 1.1)}">
        <div style="font-size:40px;font-weight:700;color:var(--saffron)">${h}</div><div class="label" style="font-size:27px">${b}</div></div>`).join("");
  },

  end(t) {
    return `<div class="title-xl" style="top:300px;${style(t, 0.2, 30, 0.9)}">tally<i>agent</i></div>
      <div class="tag" style="top:500px;${style(t, 1.2)}">Your Tally. Two clients. Two weeks.</div>`;
  },
};

// --- the frame ------------------------------------------------------------------

function sceneAt(time) {
  for (const s of T.scenes) if (time < s.start + s.duration) return s;
  return T.scenes[T.scenes.length - 1];
}

// The voice needs "G S T R two B"; a reader needs "GSTR-2B".
const SPELLED = [
  [/G S T R two B/g, "GSTR-2B"], [/G S T R one/g, "GSTR-1"], [/\bG S T\b/g, "GST"],
  [/\bA I\b/g, "AI"], [/\bC A\b/g, "CA"], [/hands off/g, "hands-off"],
  [/hash chained/g, "hash-chained"], [/follow up/g, "follow-up"], [/Sixty five percent/g, "65%"],
];
const written = (say) => SPELLED.reduce((text, [from, to]) => text.replace(from, to), say);

function captionHtml(s, local) {
  const words = written(s.say).split(/\s+/);
  const into = (local - (s.voice_at - s.start)) / s.voice_seconds;
  const n = Math.floor(clamp(into) * words.length + 0.5);
  if (into < -0.05) return "";
  return `<span class="said">${esc(words.slice(0, n).join(" "))}</span> <span class="unsaid">${esc(words.slice(n).join(" "))}</span>`;
}

window.render = function render(time) {
  const s = sceneAt(time);
  const local = time - s.start;
  const fadeIn = clamp(local / 0.35), fadeOut = clamp((s.duration - local) / 0.35);
  const body = (SCENES[s.id] || (() => ""))(local);
  stage.innerHTML = `<div style="position:absolute;inset:0;opacity:${Math.min(fadeIn, fadeOut)}">${body}</div>
    <div class="brand">tally<i>agent</i></div>
    <div class="caption">${captionHtml(s, local)}</div>
    <div class="progress" style="width:${(time / T.total) * 100}%"></div>`;
};

window.TOTAL = T.total;
render(new URLSearchParams(location.search).get("t") ? Number(new URLSearchParams(location.search).get("t")) : 0);
