// MyDeskAI page. Talks to Python through window.pywebview.api (see app.py, class
// Api) and polls api.poll() for what happened in between. All text from the AI
// or from a text message goes in with textContent, never as HTML.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const S = { values: {}, running: false, model: "", steps: [], sms: [], unseen: 0,
            view: "chat", waiting: false, timeout: 180, log: [] };
let api = null;

// (label, what it puts in the box) - the same words work by text message
const COMMANDS = [
  ["New chat", "נייע שמועס"], ["Help", "help"], ["Which model is running?", "וועלכע מאדעל"], null,
  ["Continue my terminal work", "terminal"], ["Temp / casual chat", "temp"], null,
  ["Use Opus 5", "model opus 5"], ["Use Fable 5", "model fable"], ["Use Sonnet 5", "model sonnet"],
  ["Use Haiku", "model haiku"], null,
  ["Switch to Gemini", "gemini"], ["Gemini Flash-Lite", "דשעמיני פלעש לייט"],
  ["List Gemini models", "דשעמיני מאדעלן"], ["Switch to Claude", "claude"],
];
const STARTERS = [
  ["", "Summarize my unread e-mail", "Reads today's inbox and sums it up",
   "Summarize my unread e-mails from today."],
  ["", "What is filling my C: drive?", "Finds the biggest folders and files",
   "What is taking up the most space on my C: drive?"],
  ["", "Find a file for me", "Searches this computer by name or content",
   "Help me find a file on this computer. Ask me what it is."],
  ["", "וואס קענסטו טון?", "א קורצע ליסטע פון וואס איך קען",
   "וואס קענסטו טון? גיב מיר א קורצע ליסטע."],
];
const ICONS = { terminal: "", file: "", folder: "", globe: "",
                mail: "", cloud: "", code: "", plug: "",
                phone: "", spark: "", done: "", busy: "" };
const CONNS = [["gmail_enabled", "", "Gmail"], ["sms_enabled", "", "SMS"],
               ["gdrive_enabled", "", "Drive"], ["github_enabled", "", "GitHub"],
               ["custom_enabled", "", "Web API"]];

// ------------------------------------------------------------------ helpers
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const hhmm = (d = new Date()) => d.toTimeString().slice(0, 5);
function prettyPhone(n) {
  const d = String(n || "").replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
  return d.length === 10 ? `${d.slice(0, 3)}-${d.slice(3, 6)}-${d.slice(6)}` : String(n || "?");
}
function prettyModel(label) {
  // "Claude - claude-opus-5-5" -> "Claude Opus 5.5", "Gemini - gemini-3.5-flash" -> "Gemini 3.5 Flash"
  let [brain, id] = String(label || "").split(" - ");
  if (id === undefined) { id = brain; brain = ""; }
  id = id.replace(/^(claude|gemini)-/, "").replace(/(\d)-(\d)/g, "$1.$2").replace(/-/g, " ")
    .replace(/\b([a-z])/g, (m) => m.toUpperCase());
  return brain ? `${brain} ${id}` : id;
}
function toast(text, ms = 3400) {
  const t = el("div", "toast", text);
  $("#layer").appendChild(t);
  setTimeout(() => t.remove(), ms);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = el("textarea"); ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); ta.remove();
  }
  toast("Copied.", 1500);
}

// A modal. buttons: [{label, primary, onClick}] - each closes it unless it returns false.
function modal({ title, text, pre, buttons = [{ label: "OK", primary: true }], wide, onOpen }) {
  const scrim = el("div", "scrim");
  const m = el("div", "modal" + (wide ? " wide" : ""));
  m.appendChild(el("h2", "", title || ""));
  if (text) m.appendChild(el("div", "mtext", text));
  if (pre) m.appendChild(el("pre", "", pre));
  const acts = el("div", "acts");
  const close = () => scrim.remove();
  for (const b of buttons) {
    const btn = el("button", b.primary ? "primary" : "ghost", b.label);
    btn.onclick = async () => { if ((await (b.onClick || (() => {}))()) !== false) close(); };
    acts.appendChild(btn);
  }
  m.appendChild(acts);
  scrim.appendChild(m);
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim && !m.dataset.sticky) close(); });
  $("#layer").appendChild(scrim);
  const first = acts.querySelector(".primary") || acts.querySelector("button");
  if (first) first.focus();
  if (onOpen) onOpen(m, acts, close);
  return { el: m, close };
}

// ------------------------------------------------------------------ settings
function fill(values) {
  for (const inp of $$("[data-k]")) {
    const k = inp.dataset.k;
    if (!(k in values)) continue;
    if (inp.classList.contains("tile")) inp.classList.toggle("on", values[k] === inp.dataset.v);
    else if (inp.classList.contains("dd")) ddSet(inp, values[k] ?? "");
    else if (inp.type === "checkbox") inp.checked = !!values[k];
    else inp.value = values[k] ?? "";
  }
  applyRules();
}
function collect() {
  const v = { ...S.values };
  for (const inp of $$("[data-k]")) {
    const k = inp.dataset.k;
    if (inp.classList.contains("tile")) { if (inp.classList.contains("on")) v[k] = inp.dataset.v; }
    else if (inp.classList.contains("dd")) v[k] = inp.dataset.value || "";
    else if (inp.type === "checkbox") v[k] = inp.checked;
    else v[k] = inp.value;
  }
  return v;
}
function matches(rule, v) {
  const [k, want] = rule.split("=");
  return want === undefined ? !!v[k] : String(v[k]) === want;
}
function applyRules() {
  const v = collect();
  for (const e of $$("[data-show]")) e.classList.toggle("hidden", !matches(e.dataset.show, v));
  for (const e of $$("[data-enable]")) e.disabled = !matches(e.dataset.enable, v);
  renderDesk(v);
}

// ------------------------------------------------------------------ the desk
function renderDesk(v = S.values) {
  const st = $("#state");
  st.className = "state" + (S.running ? "" : " idle");
  $("b", st).textContent = S.running ? "Running" : "Not running";
  const btn = $("#engineBtn");
  btn.textContent = S.running ? "Stop" : "Start";
  btn.className = S.running ? "" : "go";
  const where = v.access_mode === "folder" ? "one folder" : "whole computer";
  $("#meta").textContent = S.running ? `${S.model}, ${where}` :
    "Starts by itself when you send a message.";
  $("span", $("#modelChip")).textContent = S.model || "Model";
  $("span", $("#effortChip")).textContent = "Effort: " + effortName(v.effort);
  const conns = $("#conns");
  conns.replaceChildren();
  for (const [k, ic, name] of CONNS) {
    const c = el("span", "conn" + (v[k] ? "" : " off"));
    c.append(Object.assign(el("i", "ic"), { textContent: ic }), document.createTextNode(name));
    conns.appendChild(c);
  }
  $("#nokey").classList.toggle("hidden", !(v.ai_backend === "api" && !String(v.anthropic_api_key || "").trim()));
  renderSmsHead(v);
}
function renderSteps() {
  const box = $("#steps");
  box.replaceChildren();
  if (!S.steps.length) {
    box.appendChild(el("div", "empty", "Nothing yet. Ask me something and each step shows up here as I work."));
    return;
  }
  const ul = el("ul", "steps");
  for (const s of S.steps.slice(-6)) {
    const li = el("li", s.now ? "now" : "");
    const dot = el("span", "dot");
    dot.appendChild(Object.assign(el("i", "ic"), { textContent: s.now ? ICONS.busy : ICONS.done }));
    const what = el("span", "what");
    what.appendChild(el("span", "", s.title));
    what.appendChild(el("small", "", [s.detail, s.time].filter(Boolean).join(", ")));
    li.append(dot, what);
    ul.appendChild(li);
  }
  box.appendChild(ul);
}
function stepStart(title) {
  S.steps = [{ title, detail: "", time: hhmm(), now: true }];
  renderSteps();
}
function stepAdd(ev) {
  for (const s of S.steps) s.now = false;
  S.steps.push({ title: ev.title, detail: ev.detail, time: hhmm(), now: S.waiting });
  if (S.steps.length > 30) S.steps.shift();
  renderSteps();
}
function stepsDone() {
  for (const s of S.steps) s.now = false;
  renderSteps();
}
function setState(r) {
  if (!r) return;
  if ("running" in r) S.running = !!r.running;
  if (r.model) S.model = r.model;
  renderDesk(collect());
}

// ------------------------------------------------------------------ model dropdowns
const SUB_DEFAULT = { id: "", label: "Claude Code's default", note: "Whatever the Claude Code CLI is set to" };
// "" = send no effort: the model's own default (medium on Opus 5.5, high on most others).
const EFFORTS = [
  { id: "", label: "Model's default", note: "Let the model decide" },
  { id: "low", label: "Low", note: "Fastest and cheapest - fine for simple questions" },
  { id: "medium", label: "Medium", note: "Balanced" },
  { id: "high", label: "High", note: "Thinks harder - good for most real work" },
  { id: "xhigh", label: "Extra high", note: "For coding and long, many-step tasks" },
  { id: "max", label: "Max", note: "As hard as it can - slowest, uses the most" },
];
const effortName = (id) => (EFFORTS.find((e) => e.id === (id || "")) || EFFORTS[0]).label;
function modelName(id) {
  if (!id) return "";
  const m = (S.models || []).find((x) => x.id === id);
  return m ? m.label : prettyModel(id);
}
function ddSet(btn, id) {
  btn.dataset.value = id || "";
  btn.replaceChildren();
  const t = el("span", "ddtext");
  if (btn.dataset.dd === "effort") {
    const e = EFFORTS.find((x) => x.id === (id || "")) || EFFORTS[0];
    t.append(el("b", "", e.label), el("small", "", e.note));
  } else {
    const m = (S.models || []).find((x) => x.id === id);
    const sub = btn.dataset.dd === "sub";
    const label = id ? (m ? m.label : prettyModel(id)) : (sub ? SUB_DEFAULT.label : "Choose a model");
    t.append(el("b", "", label), el("small", "", id ? (m && m.note ? m.note : id) : (sub ? SUB_DEFAULT.note : "")));
  }
  btn.append(t, Object.assign(el("i", "ic ddchev"), { textContent: "" }));
}
function openDropdown(anchor, kind, current, onPick, above) {
  closePopover();
  const r = anchor.getBoundingClientRect();
  const p = el("div", "popover ddlist");
  p.id = "pop";
  const list = kind === "effort" ? EFFORTS.slice()
    : (kind === "sub" ? [SUB_DEFAULT] : []).concat(S.models || []);
  if (current && !list.some((m) => m.id === current))
    list.push({ id: current, label: prettyModel(current), note: current });
  for (const m of list) {
    const b = el("button", "ddopt" + (m.id === current ? " on" : ""));
    const t = el("span", "ddtext");
    t.append(el("b", "", m.label), el("small", "", m.note || m.id));
    b.append(t, Object.assign(el("i", "ic"), { textContent: m.id === current ? "" : "" }));
    b.onclick = (e) => { e.stopPropagation(); closePopover(); onPick(m.id); };
    p.appendChild(b);
  }
  document.body.appendChild(p);
  p.style.minWidth = Math.max(r.width, 280) + "px";
  p.style.left = Math.max(10, Math.min(r.left, innerWidth - p.offsetWidth - 12)) + "px";
  const h = p.getBoundingClientRect().height;
  const below = r.bottom + 6;
  p.style.top = (above || below + h > innerHeight - 10 ? Math.max(10, r.top - h - 6) : below) + "px";
  const on = $(".ddopt.on", p);
  if (on) on.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------ chat
function showHello() {
  const h = new Date().getHours();
  $("#greet").textContent = h < 12 ? "גוט מארגן" : h < 17 ? "גוטן טאג" : "גוטן אוונט";
  $("#hello").classList.toggle("hidden", $("#thread").children.length > 0);
}
function scrollDown() {
  const sc = $("#chatScroll");
  requestAnimationFrame(() => (sc.scrollTop = sc.scrollHeight));
}
function addMe(text) {
  const b = el("div", "me", text);
  b.dir = "auto";
  $("#thread").appendChild(b);
  showHello(); scrollDown();
}
function addAI(text, isErr) {
  dropTyping();
  const a = el("div", "ai" + (isErr ? " err" : ""));
  const who = el("div", "who");
  who.append(el("b", "", "MyDeskAI"), document.createTextNode(hhmm()));
  const cp = el("button", "copy");
  cp.append(Object.assign(el("i", "ic"), { textContent: "" }), document.createTextNode("Copy"));
  cp.onclick = () => copyText(text);
  who.appendChild(cp);
  const t = el("div", "text", text);
  t.dir = "auto";
  a.append(who, t);
  if (isErr) {
    const sl = el("button", "pill errlogs");
    sl.append(Object.assign(el("i", "ic"), { textContent: "" }), document.createTextNode("Send logs"));
    sl.onclick = () => openSendLogs();
    a.appendChild(sl);
  }
  a.dir = /[֐-׿]/.test(text.slice(0, 80)) && !/[A-Za-z]/.test(text.slice(0, 12)) ? "rtl" : "ltr";
  $("#thread").appendChild(a);
  showHello(); scrollDown();
}
function showTyping() {
  dropTyping();
  const a = el("div", "ai");
  a.id = "typing";
  const who = el("div", "who");
  who.append(el("b", "", "MyDeskAI"), document.createTextNode("working…"));
  const dots = el("div", "typing");
  dots.append(el("i"), el("i"), el("i"));
  a.append(who, dots);
  $("#thread").appendChild(a);
  scrollDown();
}
function dropTyping() { const t = $("#typing"); if (t) t.remove(); }

async function send(text) {
  text = (text ?? $("#box").value).trim();
  if (!text || S.sending) return;
  S.sending = true;
  $("#sendBtn").disabled = true;
  const r = await api.send(text, collect());
  S.sending = false;
  $("#sendBtn").disabled = false;
  if (!r.ok) {
    if (r.reason === "empty") return;
    modal({ title: r.reason === "no_key" ? "One step before the first message" : "Could not start",
            text: r.message,
            buttons: r.reason === "no_key"
              ? [{ label: "Later" }, { label: "Open settings", primary: true, onClick: () => goto("settings:engine") }]
              : [{ label: "OK", primary: true }] });
    return;                      // the text stays in the box
  }
  if ($("#box").value.trim() === text) { $("#box").value = ""; grow(); }
  setState(r);
  addMe(text);
  S.waiting = true;
  showTyping();
  stepStart("Reading your message");
}
function grow() {
  const b = $("#box");
  b.style.height = "auto";
  b.style.height = Math.min(b.scrollHeight, 180) + "px";
}

function openCommands() {
  closePopover();
  const r = $("#cmdBtn").getBoundingClientRect();
  const p = el("div", "popover");
  p.id = "pop";
  const act = (label, fn, hint) => {
    const b = el("button", "", label);
    if (hint) b.appendChild(el("small", "", hint));
    b.onclick = () => { closePopover(); fn(); };
    p.appendChild(b);
  };
  act("Open Chrome for the AI", async () => { const x = await api.open_chrome(); toast(x.message); });
  act("Clear chat", newChat);
  p.appendChild(el("hr"));
  for (const c of COMMANDS) {
    if (!c) { p.appendChild(el("hr")); continue; }
    act(c[0], () => { $("#box").value = c[1]; grow(); $("#box").focus(); },
        /[֐-׿]/.test(c[1]) ? c[1] : "");
  }
  document.body.appendChild(p);
  const h = p.getBoundingClientRect().height;
  p.style.left = r.left + "px";
  p.style.top = Math.max(10, r.top - h - 8) + "px";
}
function closePopover() { const p = $("#pop"); if (p) p.remove(); }
async function newChat() {
  $("#thread").replaceChildren();
  S.waiting = false;
  S.steps = []; renderSteps();
  await api.clear_chat();
  showHello();
}

// ------------------------------------------------------------------ send logs
// Shows exactly what will be sent (secrets already taken out by Python), lets
// the user add a note, then sends. If sending fails, Python saves the same
// report on the Desktop and says where.
async function openSendLogs() {
  const pv = await api.logs_preview();
  if (!pv.ok) { modal({ title: "Send logs", text: pv.message }); return; }
  let note;
  modal({
    title: "Send logs to the developer",
    text: "This sends MyDeskAI's activity log to the developer, so what went wrong can be found and fixed. " +
          "Keys, tokens and passwords are taken out first. The log can include what you asked the AI " +
          "and what it did - you can read all of it below before you send.",
    pre: pv.text,
    wide: true,
    onOpen: (m) => {
      note = el("textarea", "field");
      note.dir = "auto";
      note.rows = 3;
      note.style.minHeight = "70px";
      note.style.marginTop = "12px";
      note.placeholder = "What happened? (optional, Yiddish or English)";
      m.insertBefore(note, $("pre", m));
      note.focus();
    },
    buttons: [
      { label: "Cancel" },
      { label: "Save a copy", onClick: async () => {
          const r = await api.save_logs(note.value);
          toast(r.message, 6000);
        } },
      { label: "Send", primary: true, onClick: async () => {
          toast("Sending…", 1500);
          const r = await api.send_logs(note.value);
          if (r.ok)
            modal({ title: "Sent - thank you",
                    text: `The log arrived. Its number is ${r.id}.\n\n` +
                          "If you write about the problem on iVelt, mention this number so the right log is found." });
          else
            modal({ title: "Could not send", text: r.message });
        } },
    ],
  });
}

// ------------------------------------------------------------------ text messages
function renderSmsHead(v = S.values) {
  $("#smsModel").textContent = S.model || "—";
  $("#smsBridge").textContent = v.sms_enabled ? (S.running ? "On, listening" : "On (starts with the agent)") : "Off";
  const nums = String(v.allowed_numbers || "").split(/[,\s]+/).filter(Boolean).map(prettyPhone);
  $("#smsWho").textContent = !v.sms_enabled ? "Turn it on in Settings, under Connectors."
    : nums.length ? "Answers " + nums.join(", ") : "Answers anyone who texts your Google Voice number.";
}
function renderSms() {
  const box = $("#convos");
  box.replaceChildren();
  if (!S.sms.length) {
    const b = el("div", "blank");
    b.append(Object.assign(el("i", "ic"), { textContent: "" }),
             document.createTextNode("No text messages yet. When someone texts the AI, the question and the answer show up here."));
    box.appendChild(b);
    return;
  }
  for (const e of [...S.sms].reverse()) {
    const card = el("div", "ex");
    const h = el("div", "h");
    const when = e.time ? new Date(e.time) : null;
    h.append(el("b", "", prettyPhone(e.from)),
             el("span", "", when ? when.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + hhmm(when) : ""));
    if (e.model) h.appendChild(el("span", "tag", prettyModel(e.model) + (e.mode === "terminal" ? ", terminal" : "")));
    const q = el("div", "q", e.text || "");
    q.dir = "auto";
    const a = el("div", "a", e.reply || "");
    a.dir = "auto";
    card.append(h, q, a);
    if ((e.reply || "").length > 360) {
      a.classList.add("clip");
      const more = el("button", "more", "Show the whole answer");
      more.onclick = () => { a.classList.remove("clip"); more.remove(); };
      card.appendChild(more);
    }
    box.appendChild(card);
  }
}
function badge() {
  const b = $("#smsBadge");
  b.textContent = S.unseen;
  b.classList.toggle("hidden", !S.unseen);
}

// ------------------------------------------------------------------ views
function showView(name) {
  S.view = name;
  for (const v of $$(".view")) v.classList.toggle("hidden", v.id !== "view-" + name);
  for (const n of $$(".nav")) n.classList.toggle("on", n.dataset.view === name);
  if (name === "sms") { S.unseen = 0; badge(); renderSms(); }
  if (name === "chat") $("#box").focus();
}
function showTab(tab) {
  for (const b of $$("#tabs button")) b.classList.toggle("on", b.dataset.tab === tab);
  for (const p of $$("[data-pane]")) p.classList.toggle("hidden", p.dataset.pane !== tab);
}
function goto(where) {
  const [view, tab] = where.split(":");
  showView(view);
  if (tab) showTab(tab);
}

// ------------------------------------------------------------------ events from Python
function handle(ev) {
  switch (ev.kind) {
    case "chat":
      S.waiting = false;
      addAI(ev.text, /^(\(error|⚠)/.test(ev.text));
      stepsDone();
      break;
    case "log":
      S.log.push(ev.line);
      if (S.log.length > 400) S.log.shift();
      if (!$("#logPanel").classList.contains("hidden")) {
        $("#logPanel").textContent = S.log.join("\n");
        $("#logPanel").scrollTop = 1e9;
      }
      break;
    case "step": stepAdd(ev); break;
    case "sms":
      S.sms.push(ev.entry);
      if (S.sms.length > 80) S.sms.shift();
      if (S.view === "sms") renderSms(); else { S.unseen++; badge(); }
      break;
    case "status":
      setState(ev);
      if (ev.note) { toast(ev.note); dropTyping(); S.waiting = false; stepsDone(); }
      break;
    case "approve": approval(ev); break;
    case "approve_gone": { const m = $(`[data-approval="${ev.id}"]`); if (m) m.closest(".scrim").remove(); break; }
    case "update":
      modal({ title: `Version ${ev.info.tag} is ready`,
              text: `You have v${S.version}.\n\n${ev.info.notes || ""}`,
              buttons: [{ label: "Not now" },
                        { label: "Install and restart", primary: true,
                          onClick: async () => { const r = await api.do_update(ev.info); toast(r.message, 6000); } }] });
      break;
    case "info": modal({ title: ev.title || "MyDeskAI", text: ev.text }); break;
    case "toast": toast(ev.text, 5000); break;
    case "google": $("#googleStatus").textContent = ev.text; toast(ev.text, 5000); break;
    case "quit": api.quit(); break;
  }
}
function approval(ev) {
  let left = ev.timeout || S.timeout;
  modal({
    title: "Allow this command?",
    text: "The AI wants to run a command on your restricted list. It waits for your answer; " +
          "no answer counts as No.",
    pre: ev.command,
    buttons: [{ label: "Deny", onClick: () => api.answer_approval(ev.id, false) },
              { label: "Allow", primary: true, onClick: () => api.answer_approval(ev.id, true) }],
    onOpen: (m, acts, close) => {
      m.dataset.approval = ev.id;
      m.dataset.sticky = "1";
      const c = el("span", "count");
      acts.prepend(c);
      const tick = () => {
        if (!m.isConnected) return;
        c.textContent = `No answer in ${left}s counts as No`;
        if (left-- <= 0) { close(); return; }
        setTimeout(tick, 1000);
      };
      tick();
    },
  });
}
let polling = false;
async function pump() {
  if (polling) return;
  polling = true;
  try { for (const ev of await api.poll()) { try { handle(ev); } catch (e) { console.error(e); } } }
  catch (e) { console.error(e); }
  polling = false;
}

// ------------------------------------------------------------------ wiring
function wire() {
  for (const n of $$(".nav")) n.onclick = () => showView(n.dataset.view);
  for (const b of $$("#tabs button")) b.onclick = () => showTab(b.dataset.tab);
  for (const b of $$("[data-goto]")) b.onclick = () => goto(b.dataset.goto);
  for (const t of $$(".tile")) t.onclick = () => {
    for (const o of $$(`.tile[data-k="${t.dataset.k}"]`)) o.classList.toggle("on", o === t);
    applyRules();
  };
  for (const inp of $$("[data-k]")) if (!inp.classList.contains("tile")) inp.addEventListener("change", applyRules);
  for (const b of $$("[data-pick]")) b.onclick = async () => {
    const [kind, key] = b.dataset.pick.split(":");
    const path = await api.pick(kind);
    if (path) { $(`[data-k="${key}"]`).value = path; applyRules(); }
  };

  const box = $("#box");
  box.addEventListener("input", grow);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $("#sendBtn").onclick = () => send();
  $("#cmdBtn").onclick = (e) => { e.stopPropagation(); $("#pop") ? closePopover() : openCommands(); };
  document.addEventListener("click", (e) => { if (!e.target.closest("#pop")) closePopover(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closePopover();
    const s = $$(".scrim").pop();
    if (s && !$(".modal", s).dataset.sticky) s.remove();
  });
  $("#newChat").onclick = newChat;
  $("#modelChip").onclick = (e) => {
    e.stopPropagation();
    if ($("#pop")) { closePopover(); return; }
    const v = collect();
    const sub = v.ai_backend === "claude_code";
    openDropdown($("#modelChip"), sub ? "sub" : "api", sub ? v.claude_code_model : v.model, async (id) => {
      const r = await api.set_model(id, collect());
      if (!r.ok) { toast(r.message); return; }
      S.values = r.values; fill(S.values); setState(r);
      toast("From now on: " + (modelName(id) || "Claude Code's default") + ".");
    }, true);
  };
  $("#effortChip").onclick = (e) => {
    e.stopPropagation();
    if ($("#pop")) { closePopover(); return; }
    openDropdown($("#effortChip"), "effort", collect().effort || "", async (id) => {
      const r = await api.set_effort(id, collect());
      if (!r.ok) { toast(r.message); return; }
      S.values = r.values; fill(S.values); setState(r);
      toast("From now on: effort " + effortName(id).toLowerCase() + ".");
    }, true);
  };
  for (const b of $$(".dd")) b.onclick = (e) => {
    e.stopPropagation();
    if ($("#pop")) { closePopover(); return; }
    openDropdown(b, b.dataset.dd, b.dataset.value || "", (id) => { ddSet(b, id); applyRules(); });
  };
  $("#logBtn").onclick = () => {
    const p = $("#logPanel");
    p.classList.toggle("hidden");
    $("#logBtn").classList.toggle("on", !p.classList.contains("hidden"));
    p.textContent = S.log.join("\n") || "Nothing logged yet.";
    p.scrollTop = 1e9;
  };
  $("#engineBtn").onclick = async () => {
    const r = S.running ? await api.stop() : await api.start(collect());
    if (!r.ok && r.reason === "no_key") {
      modal({ title: "One step before starting", text: r.message,
              buttons: [{ label: "Later" }, { label: "Open settings", primary: true, onClick: () => goto("settings:engine") }] });
      return;
    }
    if (!r.ok) { modal({ title: "Could not start", text: r.message }); return; }
    setState(r);
    toast(S.running ? "Agent started." : "Agent stopped.");
  };
  $("#saveBtn").onclick = async () => {
    const r = await api.save_settings(collect());
    $("#saveNote").textContent = r.message;
    setTimeout(() => ($("#saveNote").textContent = ""), 4000);
    if (r.ok) { S.values = collect(); setState(r); }
  };
  $("#connectGoogle").onclick = async () => {
    const r = await api.connect_google(collect());
    $("#googleStatus").textContent = r.message;
  };
  $("#googleHelp").onclick = () => modal({ title: "Google setup, step by step", pre: S.googleHelp, wide: true });
  $("#shortcuts").onclick = async () => toast((await api.make_shortcuts()).message);
  $("#sendLogsBtn").onclick = openSendLogs;
  $("#sendLogsBtn2").onclick = openSendLogs;
  $("#checkNow").onclick = async () => {
    S.values = collect();
    await api.save_settings(S.values);
    const r = await api.check_updates(true);
    toast(r.ok ? "Checking for updates…" : r.message);
  };
}

async function boot() {
  api = window.pywebview.api;
  const d = await api.init();
  S.values = d.values; S.sms = d.sms || []; S.version = d.version;
  S.timeout = d.approvalTimeout; S.googleHelp = d.googleHelp;
  $("#credit").textContent = d.credit;
  $("#ver").textContent = "Version " + d.version;
  S.models = d.models;
  $("#restrictLead").textContent = "One phrase per line. A shell command that contains one needs your OK " +
    `first; no answer within ${Math.round(d.approvalTimeout / 60)} minutes counts as No. In ` +
    "subscription mode they are blocked outright. Autonomous mode skips this list.";
  const st = $("#starters");
  for (const [ic, title, sub, prompt] of STARTERS) {
    const it = el("button", "item");
    if (/[֐-׿]/.test(title)) it.dir = "rtl";
    const t = el("div");
    t.append(document.createTextNode(title), el("small", "", sub));
    it.append(Object.assign(el("span", "ic"), { textContent: ic }), t);
    it.onclick = () => send(prompt);
    st.appendChild(it);
  }
  wire();
  fill(S.values);
  setState(d);
  renderSteps();
  showHello();
  showView("chat");
  setInterval(pump, 250);
  api.models(S.values).then((m) => { if (m && m.choices) { S.models = m.choices; fill(collect()); } });
  if (S.values.auto_check_updates) api.check_updates(false);
}
if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
