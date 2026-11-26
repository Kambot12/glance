"use strict";
/* Model output is untrusted. Every node below is built with createElement /
   createTextNode. There is no innerHTML, no eval, no HTML parsing of model text. */

const statusEl = document.getElementById("status");
const outputEl = document.getElementById("output");
const modesEl = document.getElementById("modes");
const form = document.getElementById("followup-form");
const input = document.getElementById("followup");
const sendBtn = document.getElementById("send");

// The token arrives in the URL fragment, which browsers never send to the
// server. Strip it from the address bar once read.
function takeToken() {
  const m = /(?:^|&)t=([A-Za-z0-9_-]+)/.exec(location.hash.slice(1));
  const tok = m ? m[1] : null;
  if (tok) history.replaceState(null, "", location.pathname);
  return tok;
}

function setStatus(text, state) {
  statusEl.textContent = text;
  statusEl.dataset.state = state;
}

/* ---- safe markdown subset -> DOM ---- */

function inline(text, parent) {
  // `code`, **bold**, *italic*. Anything else stays literal text.
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    let el;
    if (tok.startsWith("`")) {
      el = document.createElement("code");
      el.textContent = tok.slice(1, -1);
    } else if (tok.startsWith("**")) {
      el = document.createElement("strong");
      el.textContent = tok.slice(2, -2);
    } else {
      el = document.createElement("em");
      el.textContent = tok.slice(1, -1);
    }
    parent.appendChild(el);
    last = m.index + tok.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function renderMarkdown(src) {
  const frag = document.createDocumentFragment();
  const lines = src.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      const body = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) body.push(lines[i++]);
      i++; // closing fence
      code.textContent = body.join("\n");
      pre.appendChild(code);
      frag.appendChild(pre);
      continue;
    }
    if (/^#{1,6}\s/.test(line)) {
      const h = document.createElement("h3");
      inline(line.replace(/^#{1,6}\s/, ""), h);
      frag.appendChild(h);
      i++;
      continue;
    }
    if (/^\s*[-*]\s/.test(line)) {
      const ul = document.createElement("ul");
      while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) {
        const li = document.createElement("li");
        inline(lines[i].replace(/^\s*[-*]\s/, ""), li);
        ul.appendChild(li);
        i++;
      }
      frag.appendChild(ul);
      continue;
    }
    if (line.trim() === "") { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() !== "" && !lines[i].startsWith("```")
           && !/^\s*[-*]\s/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])) {
      para.push(lines[i++]);
    }
    const p = document.createElement("p");
    inline(para.join(" "), p);
    frag.appendChild(p);
  }
  return frag;
}

/* ---- streaming ---- */

let current = null; // { wrapper, body, text }

function beginExchange(label) {
  outputEl.querySelector(".hint")?.remove();
  const wrapper = document.createElement("section");
  wrapper.className = "exchange";
  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent = label;
  const body = document.createElement("div");
  body.classList.add("cursor");
  wrapper.append(meta, body);
  outputEl.appendChild(wrapper);
  outputEl.setAttribute("aria-busy", "true");
  current = { wrapper, body, text: "" };
  wrapper.scrollIntoView({ block: "end", behavior: "smooth" });
}

function appendChunk(text) {
  if (!current) beginExchange("response");
  current.text += text;
  current.body.replaceChildren(renderMarkdown(current.text));
}

function endExchange() {
  if (current) current.body.classList.remove("cursor");
  outputEl.setAttribute("aria-busy", "false");
  current = null;
}

function notice(text, cls) {
  outputEl.querySelector(".hint")?.remove();
  const p = document.createElement("p");
  p.className = cls;
  p.textContent = text;
  outputEl.appendChild(p);
  p.scrollIntoView({ block: "end", behavior: "smooth" });
}

/* ---- wiring ---- */

const token = takeToken();
let socket = null;

function connect() {
  if (!token) {
    setStatus("no token", "denied");
    notice("This page needs the one-time link printed in the console (it carries the session token).", "error");
    return;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${proto}//${location.host}/ws`, ["glance.v1", "token." + token]);

  socket.onopen = () => {
    setStatus("connected", "open");
    input.disabled = false;
    sendBtn.disabled = false;
    modesEl.querySelectorAll("button").forEach((b) => (b.disabled = false));
  };
  socket.onclose = (ev) => {
    setStatus(ev.code === 1008 ? "rejected" : "disconnected", ev.code === 1008 ? "denied" : "closed");
    input.disabled = true;
    sendBtn.disabled = true;
    modesEl.querySelectorAll("button").forEach((b) => (b.disabled = true));
    if (ev.code === 1008) notice("Connection rejected: bad token or origin.", "error");
  };
  socket.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    switch (msg.type) {
      case "start":   beginExchange(msg.label || "response"); break;
      case "chunk":   appendChunk(String(msg.text ?? "")); break;
      case "end":     endExchange(); break;
      case "status":  setStatus(String(msg.text ?? ""), msg.state || "open"); break;
      case "blocked": endExchange(); notice(String(msg.text ?? "capture skipped"), "blocked"); break;
      case "error":   endExchange(); notice(String(msg.text ?? "error"), "error"); break;
    }
  };
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || !socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: "followup", text }));
  input.value = "";
});

fetch("/api/modes")
  .then((r) => r.json())
  .then(({ modes }) => {
    (modes || []).forEach((name) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = name;
      b.disabled = true;
      b.addEventListener("click", () => {
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "trigger", mode: name }));
        }
      });
      modesEl.appendChild(b);
    });
    if (socket && socket.readyState === WebSocket.OPEN) {
      modesEl.querySelectorAll("button").forEach((b) => (b.disabled = false));
    }
  })
  .catch(() => {});

connect();
