// Releases: list, make on demand, edit, download, delete.
(function () {
  const el = document.getElementById("releases");
  const kinds = { none: "label only", iso: "data CD", audio: "audio CD", cue: "BIN/CUE" };
  const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  async function load() {
    const r = await fetch("/api/releases"); if (!r.ok) return;
    const { releases } = await r.json();
    if (!releases.length) { el.innerHTML = '<p class="hint">No releases saved yet.</p>'; return; }
    el.innerHTML = "";
    releases.forEach((m) => {
      const card = document.createElement("section"); card.className = "card release";
      const img = document.createElement(m.has_label ? "img" : "div");
      if (m.has_label) { img.src = `/api/releases/${m.id}/preview.png?t=${m.updated}`; img.alt = ""; } else img.className = "noimg";
      const body = document.createElement("div"); body.className = "release-body";
      const h = document.createElement("h2"); h.textContent = m.name;
      const meta = document.createElement("p"); meta.className = "hint";
      const parts = [m.artist, kinds[m.kind] || m.kind];
      if (m.kind === "audio") parts.push(`${m.tracks} tracks, ${fmt(m.duration_s)}`);
      parts.push(m.made ? `${m.made} discs made, last on ${new Date(m.last_made * 1000).toLocaleDateString()}` : "not made yet");
      meta.textContent = parts.filter(Boolean).join(" · ");
      const row = document.createElement("div"); row.className = "row";
      const copies = document.createElement("input"); copies.type = "number"; copies.min = 1; copies.max = 50;
      copies.value = (m.job_defaults && m.job_defaults.copies) || 1; copies.className = "copies";
      const lab = document.createElement("label"); lab.append("Copies ", copies);
      const inBin = document.createElement("select");
      ["1", "2", "3"].forEach((b) => inBin.add(new Option(b, b)));
      inBin.value = String((m.job_defaults && m.job_defaults.input_bin) || 1);
      const binLab = document.createElement("label"); binLab.append("Input bin ", inBin);
      const doPrint = document.createElement("input"); doPrint.type = "checkbox"; doPrint.checked = true;
      const printLab = document.createElement("label"); printLab.append(doPrint, " Print label");
      printLab.title = "Off: burn only, for example on discs that are already printed";
      printLab.hidden = !(m.has_label && m.kind !== "none");
      const make = document.createElement("button"); make.className = "primary"; make.textContent = "Make";
      make.addEventListener("click", async () => {
        make.disabled = true;
        const r2 = await fetch(`/api/releases/${m.id}/jobs`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ copies: Number(copies.value), input_bin: Number(inBin.value),
            ...(printLab.hidden || doPrint.checked ? {} : { print: false }) }) });
        const j = await r2.json(); make.disabled = false;
        if (!r2.ok) { alert(j.error || "Failed"); return; }
        location.href = "/jobs/" + j.id;
      });
      const edit = document.createElement("a"); edit.href = "/?release=" + encodeURIComponent(m.id); edit.textContent = "Edit"; edit.className = "button";
      const zip = document.createElement("a"); zip.href = `/api/releases/${m.id}/zip`; zip.textContent = "Download (zip)"; zip.className = "button";
      const del = document.createElement("button"); del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete release "${m.name}" and all its files?`)) return;
        await fetch(`/api/releases/${m.id}`, { method: "DELETE" }); load();
      });
      row.append(lab, binLab, printLab, make, edit, zip, del);
      body.append(h, meta, row);
      card.append(img, body); el.appendChild(card);
    });
  }
  load();
})();
