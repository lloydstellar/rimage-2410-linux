// Draw the progress of a batch: counters, arm, recorders, printer and each disc.
window.renderProgress = function (el, p, opts) {
  if (!p) { el.innerHTML = '<p class="hint">No progress yet.</p>'; return; }
  const stageText = { waiting: "in input bin", loading: "to recorder", burning: "burning", burned: "burned",
    burn_failed: "burn failed", unloading: "from recorder", to_printer: "to printer", printing: "printing",
    printed: "printed", storing: "storing", done: "done", rejected: "rejected" };
  const phaseText = { preparing: "Preparing", running: "Running", done: "Done", stopped: "Stopped", error: "Stopped by an error" };
  const now = Date.now() / 1000;
  const dur = (t) => { const s = Math.max(0, Math.round(now - t)); return s < 60 ? `${s} s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} min`; };
  const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = p.total ? Math.round(((p.done + p.rejected) / p.total) * 100) : 0;
  let h = "";
  if (p.problem) {
    const q = p.problem; const s = q.sense;
    h += `<div class="pg-problem ${q.waiting ? "ask" : "note"}"><div class="pg-ptitle">${esc(q.title)}</div>
      <div>${esc(q.message)}</div>
      ${q.waiting ? `<div class="pg-wait">Waiting for you${q.since ? ` · ${dur(q.since)}` : ""}</div>` : ""}
      ${q.waiting && q.options && q.options.length ? `<div class="pg-buttons">${q.options.map((o) =>
        `<button type="button" data-choice="${esc(o.id)}" class="${o.id === "stop" ? "danger" : ""}">${esc(o.label)}</button>`).join("")}</div>` : ""}
    </div>`;
  }
  if (p.error && p.phase === "error") h += `<div class="pg-problem note"><div class="pg-ptitle">Job stopped</div><div>${esc(p.error)}</div></div>`;
  h += `<div class="pg-head"><span class="pg-phase">${esc(phaseText[p.phase] || p.phase)}</span>
    <span><b>${p.done}</b> of ${p.total} done${p.rejected ? `, <b>${p.rejected}</b> rejected` : ""}</span></div>
    <div class="pg-bar"><div style="width:${pct}%"></div></div><div class="pg-units">`;
  h += `<div class="pg-unit ${p.arm ? "busy" : ""}"><div class="pg-name">Robot arm</div><div>${esc(p.arm || "waiting")}</div></div>`;
  Object.entries(p.drives || {}).forEach(([dev, d], i) => {
    const busy = d.state !== "idle";
    h += `<div class="pg-unit ${busy ? "busy" : ""}"><div class="pg-name">Recorder ${i + 1} <span class="hint">${esc(dev)}</span></div>
      <div>${esc(d.state)}${d.disc ? ` · disc ${d.disc}` : ""}${busy ? ` · ${dur(d.since)}` : ""}</div></div>`;
  });
  if (p.printing) {
    const pr = p.printer || {}; const busy = pr.state && pr.state !== "idle";
    h += `<div class="pg-unit ${busy ? "busy" : ""}"><div class="pg-name">Printer</div>
      <div>${esc(pr.state || "idle")}${pr.disc ? ` · disc ${pr.disc}` : ""}${busy ? ` · ${dur(pr.since)}` : ""}</div>
      ${pr.info ? `<div class="hint">${esc(pr.info)}</div>` : ""}</div>`;
  }
  h += `</div><div class="pg-discs">`;
  (p.discs || []).forEach((d) => {
    h += `<div class="pg-disc st-${esc(d.stage)}" title="${esc(stageText[d.stage] || d.stage)}: ${esc(d.where)}">
      <b>${d.n}</b><span>${esc(stageText[d.stage] || d.stage)}</span></div>`;
  });
  h += `</div>`;
  // do not redraw when nothing changed, otherwise buttons disappear under the mouse
  const key = JSON.stringify({ ...p, updated: 0, now: Math.floor(now / 5) });
  if (el._pgKey === key) return;
  el._pgKey = key;
  el.innerHTML = h;
  if (opts && opts.onAnswer) el.querySelectorAll(".pg-buttons button").forEach((b) => {
    b.addEventListener("click", () => { el.querySelectorAll(".pg-buttons button").forEach((x) => { x.disabled = true; });
      opts.onAnswer(b.dataset.choice); });
  });
};
window.answerJob = async function (jobId, choice) {
  const r = await fetch(`/api/jobs/${jobId}/answer`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ choice }) });
  if (!r.ok) { const j = await r.json().catch(() => ({})); alert(j.error || "Answer not processed"); }
};
