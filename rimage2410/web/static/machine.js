// Machine: status of the real machine (mode 'real') or of the simulated machine (dry run).
(function () {
  function facts(el, pairs) { el.innerHTML = ""; pairs.forEach(([k, v]) => { const dt = document.createElement("dt"); dt.textContent = k; const dd = document.createElement("dd"); dd.textContent = v; el.append(dt, dd); }); }
  async function load() {
    const r = await fetch("/api/machine"); if (!r.ok) return;
    const m = await r.json();
    const now = document.getElementById("now");
    if (m.progress) {
      window.renderProgress(now, m.progress, { onAnswer: (choice) => window.answerJob(m.current_job, choice).then(load) });
      if (!now.querySelector(".pg-jobref")) now.insertAdjacentHTML("afterbegin", `<p class="hint pg-jobref">Job <a href="/jobs/${m.current_job}">${m.current_job}</a></p>`);
    } else { now._pgKey = null; now.innerHTML = '<p class="hint">No job running.</p>'; }
    if (m.mode === "real") renderReal(m); else renderSim(m);
    const sup = document.getElementById("supplies");
    if (m.supplies_error) { facts(sup, [["Error", m.supplies_error]]); }
    else facts(sup, [["Ribbon", m.ribbon_name],
      ["Prints left", m.ribbon_remaining == null ? "unknown" : `${m.ribbon_remaining} discs` + (m.ribbon_percent == null ? "" : ` (${m.ribbon_percent}%)`)],
      ["Retransfer film", !m.film_present ? "not installed" : (m.film_percent == null ? "unknown" : `${m.film_percent}%`)
        + (m.film_remaining == null ? "" : `, ${m.film_remaining} prints left`)],
      ["Printer counter", m.printer_counter == null ? "unknown" : String(m.printer_counter)],
      ["Printer status", m.printer_state || ""]]);
  }
  function renderBins(m) {
    const bins = document.getElementById("bins"); bins.innerHTML = "";
    Object.entries(m.bins || {}).forEach(([n, c]) => {
      const d = document.createElement("div"); d.className = "bin";
      const b = document.createElement("b"); b.textContent = c == null ? "?" : String(c);
      d.append(b, document.createTextNode(`bin ${n}`)); bins.appendChild(d);
    });
  }
  function renderSim(m) {
    renderBins(m);
    const drives = Object.entries(m.drives).map(([e, d]) => [`Recorder ${e}`, `${d.tray_open ? "tray open" : "tray closed"}, ${d.disc ? "disc" : "empty"}`]);
    facts(document.getElementById("arm"), [["Gripper", m.gripper ? "holding a disc" : "empty"], ...drives,
      ["Loader", `${m.changer_sense}: ${m.changer_text}`], ["Job", m.current_job || "none"]]);
    facts(document.getElementById("printer"), [["Status", `${m.printer_sense}: ${m.printer_text}`],
      ["Printer tray", m.drawer_open ? "open" : "closed"], ["Disc in printer", m.printer_disc ? "yes" : "no"], ["Mode", m.awake ? "awake" : "standby"]]);
  }
  function renderReal(m) {
    if (m.busy) {   // during a job the web app does not read the machine; see the progress above
      facts(document.getElementById("arm"), [["Job", m.current_job || "running"], ["Status", "see the progress above"]]);
      facts(document.getElementById("printer"), [["Status", "see the progress above"]]);
      return;
    }
    if (m.connected === false) {
      facts(document.getElementById("arm"), [["Machine", m.error || "not found"], ["What to do", "Switch the Rimage on and check the USB cable. The web app reconnects by itself."]]);
      facts(document.getElementById("printer"), [["Status", "not connected"]]);
      document.getElementById("bins").innerHTML = "";
      return;
    }
    renderBins(m);
    const tray = (v) => v == null ? "tray unknown" : (v ? "tray open" : "tray closed");
    const drives = Object.entries(m.drives || {}).map(([e, d]) => [`Recorder ${e} (${d.device})`, d.error ? `error: ${d.error}` : tray(d.tray_open)]);
    const arm = [["Gripper", m.gripper ? "holding a disc" : "empty"], ...drives,
      ["Loader", m.changer_text == null ? "unknown" : `${m.changer_sense}: ${m.changer_text}`]];
    if (m.error) arm.push(["Read error", m.error]);
    facts(document.getElementById("arm"), arm);
    facts(document.getElementById("printer"), [["Status", m.printer_text == null ? "unknown" : `${m.printer_sense}: ${m.printer_text}`],
      ["Disc in printer", m.printer_disc ? "yes" : "no"],
      ["Heat roller", m.roller_temp == null ? "unknown" : `${m.roller_temp} °C`]]);
  }
  const resetBtn = document.getElementById("reset");
  if (resetBtn) resetBtn.addEventListener("click", async () => {
    const r = await fetch("/api/machine/reset", { method: "POST" });
    if (!r.ok) { const j = await r.json(); alert(j.error || "Failed"); }
    load();
  });
  const simRibbon = document.getElementById("sim-ribbon");
  if (simRibbon) simRibbon.addEventListener("change", async () => {
    await fetch("/api/machine/sim-ribbon", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ribbon: simRibbon.value }) });
    load();
  });
  const simFault = document.getElementById("sim-fault");
  if (simFault) simFault.addEventListener("click", async () => {
    const sel = document.getElementById("sim-fault-kind");
    const r = await fetch("/api/machine/sim-fault", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fault: sel.value }) });
    if (!r.ok) { const j = await r.json(); alert(j.error || "Failed"); }
    load();
  });
  load(); setInterval(load, 2000);
})();
