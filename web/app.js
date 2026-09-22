/*
 * WebRTC client for a local speech-to-speech Realtime server.
 *
 * The handshake is OpenAI's GA "calls" flow: POST an SDP offer as
 * `application/sdp`, get an SDP answer back, then carry audio on the media track
 * and events on an `oai-events` data channel.
 *
 * The latency measurements here use the same stage boundaries as
 * `localvoice.timeline`, deliberately. A dashboard that defined "LLM latency"
 * differently from the benchmark would be worse than no dashboard.
 *
 * One measurement note that matters: the browser cannot see when the *server's*
 * VAD decided you stopped talking any earlier than the `speech_stopped` event,
 * but it does know when its own microphone last carried speech. Both are shown,
 * because the gap between them is real waiting that a server-side number hides.
 */

const SERIES = ["--series-1", "--series-2", "--series-3", "--series-4"];
const STAGES = [
  { key: "vad", label: "VAD end-of-turn" },
  { key: "asr", label: "ASR" },
  { key: "llm", label: "LLM" },
  { key: "tts", label: "TTS" },
];

const el = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(name).trim();
const fmt = (v) => (v === null || v === undefined || !isFinite(v) ? "—" : v >= 1000 ? (v / 1000).toFixed(2) + " s" : Math.round(v) + " ms");

const state = {
  pc: null,
  channel: null,
  stream: null,
  callId: null,
  turns: [],
  current: null,
  speechActive: false,
  lastVoiceAt: null,
  vad: null,
};

function setStatus(text, cls) {
  const node = el("status");
  node.textContent = text;
  node.className = "status " + cls;
}

function log(line) {
  const node = el("events");
  node.textContent = (node.textContent + "\n" + line).split("\n").slice(-200).join("\n");
  node.scrollTop = node.scrollHeight;
}

/* ── Local voice activity ──────────────────────────────────────────────
 * Used only to timestamp when the microphone last carried speech, so the
 * page can show the same "user clock" latency the benchmark reports. It is
 * not a turn detector and never gates what is sent; the server's VAD owns
 * turn-taking.
 */
function startLocalVad(stream) {
  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);
  const buffer = new Float32Array(analyser.fftSize);

  const tick = () => {
    analyser.getFloatTimeDomainData(buffer);
    let sum = 0;
    for (let i = 0; i < buffer.length; i++) sum += buffer[i] * buffer[i];
    const rms = Math.sqrt(sum / buffer.length);
    if (rms > 0.02) state.lastVoiceAt = performance.now();
    state.vad = requestAnimationFrame(tick);
  };
  state.vad = requestAnimationFrame(tick);
  return ctx;
}

/* ── Turn accounting ─────────────────────────────────────────────────── */

function newTurn() {
  const turn = {
    index: state.turns.length + 1,
    clientSpeechEnd: state.lastVoiceAt,
    speechStopped: null,
    transcriptDone: null,
    firstToken: null,
    firstAudio: null,
    transcript: "",
    reply: "",
  };
  state.turns.push(turn);
  state.current = turn;
  return turn;
}

function current() {
  return state.current || newTurn();
}

function ms(later, earlier) {
  return later !== null && earlier !== null && later !== undefined && earlier !== undefined ? later - earlier : null;
}

function measures(turn) {
  return {
    vad: ms(turn.speechStopped, turn.clientSpeechEnd),
    asr: ms(turn.transcriptDone, turn.speechStopped),
    llm: ms(turn.firstToken, turn.transcriptDone),
    tts: ms(turn.firstAudio, turn.firstToken),
    perceived: ms(turn.firstAudio, turn.clientSpeechEnd),
  };
}

function handleEvent(event) {
  const now = performance.now();
  switch (event.type) {
    case "input_audio_buffer.speech_started":
      state.speechActive = true;
      newTurn();
      setStatus("listening", "live");
      break;
    case "input_audio_buffer.speech_stopped": {
      state.speechActive = false;
      const turn = current();
      // The last frame our own microphone carried speech is the best estimate
      // of when the person actually stopped, which precedes this event.
      turn.clientSpeechEnd = turn.clientSpeechEnd || state.lastVoiceAt;
      turn.speechStopped = now;
      setStatus("thinking", "busy");
      break;
    }
    case "conversation.item.input_audio_transcription.completed": {
      const turn = current();
      turn.transcriptDone = now;
      turn.transcript = event.transcript || "";
      renderTranscript();
      break;
    }
    case "response.output_text.delta":
    case "response.output_audio_transcript.delta": {
      const turn = current();
      if (turn.firstToken === null) turn.firstToken = now;
      turn.reply += event.delta || "";
      renderTranscript();
      break;
    }
    case "response.output_audio.delta": {
      const turn = current();
      if (turn.firstToken === null) turn.firstToken = now;
      if (turn.firstAudio === null) {
        turn.firstAudio = now;
        setStatus("speaking", "live");
        render();
      }
      break;
    }
    case "response.done":
      setStatus("connected", "ok");
      state.current = null;
      render();
      break;
    case "error":
      log("ERROR " + JSON.stringify(event.error));
      setStatus("error", "bad");
      break;
  }
}

/* ── Rendering ────────────────────────────────────────────────────────── */

function renderTranscript() {
  const host = el("transcript");
  const rows = state.turns
    .filter((t) => t.transcript || t.reply)
    .map(
      (t) =>
        '<p class="line"><span class="who you">you</span>' + escapeHtml(t.transcript || "…") + "</p>" +
        (t.reply ? '<p class="line"><span class="who bot">assistant</span>' + escapeHtml(t.reply) + "</p>" : "")
    );
  host.innerHTML = rows.length ? rows.join("") : '<p class="empty">Nothing yet. Connect and say something.</p>';
  host.scrollTop = host.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function render() {
  const done = state.turns.filter((t) => t.firstAudio !== null);
  const last = done[done.length - 1];
  if (!last) return;
  const m = measures(last);

  el("t-ttfa").textContent = fmt(m.perceived);
  el("t-asr").textContent = fmt(m.asr);
  el("t-llm").textContent = fmt(m.llm);
  el("t-tts").textContent = fmt(m.tts);

  drawBudget(m);
  renderTurns(done);
}

function drawBudget(m) {
  const host = el("budget");
  host.innerHTML = "";
  const parts = STAGES.map((s, i) => ({ ...s, ms: m[s.key], color: css(SERIES[i]) })).filter((p) => p.ms !== null && p.ms > 0);
  if (!parts.length) return;

  const total = parts.reduce((a, b) => a + b.ms, 0);
  const W = host.clientWidth || 800;
  const H = 110, L = 8, R = 8, barY = 44, barH = 24, GAP = 2;
  const scale = (W - L - R) / total;

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", H);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  let x = L;
  parts.forEach((p, i) => {
    const raw = p.ms * scale;
    const w = Math.max(2, raw - (i < parts.length - 1 ? GAP : 0));
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", roundedSide(x, barY, w, barH, 4, i === 0, i === parts.length - 1));
    path.setAttribute("fill", p.color);
    hover(path, `<b>${p.label}</b><br>${fmt(p.ms)} · ${Math.round((p.ms / total) * 100)}%`);
    svg.appendChild(path);

    if (raw > 46) {
      const label = document.createElementNS(NS, "text");
      label.setAttribute("x", x + w / 2);
      label.setAttribute("y", barY - 10);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("class", "axis");
      label.textContent = fmt(p.ms);
      svg.appendChild(label);
    }
    x += raw;
  });

  const totalLabel = document.createElementNS(NS, "text");
  totalLabel.setAttribute("x", W - R);
  totalLabel.setAttribute("y", barY + barH + 22);
  totalLabel.setAttribute("text-anchor", "end");
  totalLabel.setAttribute("class", "axis");
  totalLabel.textContent = fmt(total) + " to first audio";
  svg.appendChild(totalLabel);
  host.appendChild(svg);

  el("legend").innerHTML = parts
    .map((p) => `<span><i class="swatch" style="background:${p.color}"></i>${p.label} · ${fmt(p.ms)}</span>`)
    .join("");
}

function roundedSide(x, y, w, h, r, left, right) {
  r = Math.min(r, w / 2, h / 2);
  const rl = left ? r : 0, rr = right ? r : 0;
  return `M${x + rl},${y}h${w - rl - rr}` +
    (rr ? `a${rr},${rr} 0 0 1 ${rr},${rr}` : "") +
    `v${h - 2 * rr}` +
    (rr ? `a${rr},${rr} 0 0 1 ${-rr},${rr}` : "") +
    `h${-(w - rl - rr)}` +
    (rl ? `a${rl},${rl} 0 0 1 ${-rl},${-rl}` : "") +
    `v${-(h - 2 * rl)}` +
    (rl ? `a${rl},${rl} 0 0 1 ${rl},${-rl}` : "") + "z";
}

function hover(node, html) {
  const tip = el("tip");
  node.addEventListener("mousemove", (e) => {
    tip.innerHTML = html;
    tip.style.opacity = "1";
    let x = e.clientX + 14, y = e.clientY + 14;
    const r = tip.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 14;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  });
  node.addEventListener("mouseleave", () => { tip.style.opacity = "0"; });
}

function renderTurns(done) {
  const rows = done
    .slice()
    .reverse()
    .map((t) => {
      const m = measures(t);
      return `<tr><td>${t.index}</td><td class="say">${escapeHtml(t.transcript || "—")}</td>` +
        `<td>${fmt(m.asr)}</td><td>${fmt(m.llm)}</td><td>${fmt(m.tts)}</td><td><b>${fmt(m.perceived)}</b></td></tr>`;
    });
  el("turns").innerHTML =
    "<tr><th>#</th><th>You said</th><th>ASR</th><th>LLM</th><th>TTS</th><th>Perceived TTFA</th></tr>" + rows.join("");
}

/* ── Connection ───────────────────────────────────────────────────────── */

async function connect() {
  const base = el("endpoint").value.replace(/\/+$/, "");
  setStatus("connecting", "busy");
  el("connect").disabled = true;

  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        // The server runs its own VAD and expects speech, not processed silence.
        // Echo cancellation stays on: without it the assistant's own output
        // re-enters the microphone and triggers a barge-in against itself.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    setStatus("microphone denied", "bad");
    el("connect").disabled = false;
    log("getUserMedia failed: " + err);
    return;
  }

  startLocalVad(state.stream);

  const pc = new RTCPeerConnection();
  state.pc = pc;
  state.stream.getAudioTracks().forEach((track) => pc.addTrack(track, state.stream));

  pc.ontrack = (event) => {
    el("remote").srcObject = event.streams[0];
  };
  pc.onconnectionstatechange = () => {
    log("peer connection: " + pc.connectionState);
    if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
      setStatus(pc.connectionState, "bad");
    }
  };

  // The server expects this exact label; anything else is ignored.
  const channel = pc.createDataChannel("oai-events");
  state.channel = channel;
  channel.onopen = () => {
    setStatus("connected", "ok");
    channel.send(JSON.stringify({
      type: "session.update",
      session: {
        type: "realtime",
        audio: { input: { turn_detection: { type: "server_vad", interrupt_response: true } }, output: {} },
        instructions: "You are a voice assistant. Answer in one short sentence. Never use lists or markdown.",
      },
    }));
  };
  channel.onmessage = (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    if (event.type !== "response.output_audio.delta") log(event.type);
    handleEvent(event);
  };

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitForIceGathering(pc);

  const response = await fetch(base + "/v1/realtime/calls", {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: pc.localDescription.sdp,
  });

  if (!response.ok) {
    const detail = await response.text();
    setStatus(response.status === 503 ? "all sessions in use" : "handshake failed", "bad");
    log(`POST /v1/realtime/calls -> ${response.status}: ${detail.slice(0, 200)}`);
    await hangup();
    return;
  }

  // The Location header carries the call id, used to hang up cleanly.
  const location = response.headers.get("Location");
  state.callId = location ? location.split("/").pop() : null;
  await pc.setRemoteDescription({ type: "answer", sdp: await response.text() });
  el("hangup").disabled = false;
}

/* Trickle ICE is not part of the calls handshake: the offer is POSTed once, so
 * it has to be complete. Wait for gathering, but do not wait forever on a
 * network where a candidate type never resolves. */
function waitForIceGathering(pc, timeoutMs = 2000) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      clearTimeout(timer);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === "complete") done(); };
    const timer = setTimeout(done, timeoutMs);
    pc.addEventListener("icegatheringstatechange", check);
  });
}

async function hangup() {
  const base = el("endpoint").value.replace(/\/+$/, "");
  if (state.vad) cancelAnimationFrame(state.vad);
  if (state.callId) {
    // Tell the server, so its pipeline unit is released rather than left to drain.
    try {
      await fetch(`${base}/v1/realtime/calls/${state.callId}`, { method: "DELETE" });
    } catch (err) {
      log("hangup request failed: " + err);
    }
  }
  if (state.channel) state.channel.close();
  if (state.pc) state.pc.close();
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
  Object.assign(state, { pc: null, channel: null, stream: null, callId: null, current: null, vad: null });
  el("connect").disabled = false;
  el("hangup").disabled = true;
  setStatus("idle", "idle");
}

el("connect").addEventListener("click", connect);
el("hangup").addEventListener("click", hangup);
window.addEventListener("beforeunload", hangup);
window.addEventListener("resize", () => render());
