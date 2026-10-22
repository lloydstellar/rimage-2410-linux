// Queue: list of jobs, refreshed every 3 seconds.
(function () {
  const names = { queued: "queued", preparing: "preparing", running: "running", done: "done", failed: "failed", stopped: "stopped" };
  const kinds = { none: "print only", iso: "data CD", audio: "audio CD", cue: "BIN/CUE" };
  async function load() {
    const r = await fetch("/api/jobs"); if (!r.ok) return;
    const { jobs } = await r.json();
    const el = document.getElementById("jobs");
    if (!jobs.length) { el.innerHTML = '<p class="hint">No jobs yet.</p>'; return; }
    el.innerHTML = "";
    jobs.forEach((j) => {
      const a = document.createElement("a"); a.className = "job"; a.href = "/jobs/" + j.id;
      const img = document.createElement(j.has_label ? "img" : "div");
      if (j.has_label) { img.src = "/jobs/" + j.id + "/preview.png"; img.alt = ""; } else img.className = "noimg";
      const mid = document.createElement("div");
      const title = document.createElement("div"); title.textContent = j.title || j.id;
      const meta = document.createElement("div"); meta.className = "meta";
      meta.textContent = `${j.copies} disc(s), ${kinds[j.source_kind] || j.source_kind}${j.has_label ? ", with label" : ""} · ${new Date(j.created * 1000).toLocaleString()}`;
      mid.append(title, meta);
      const st = document.createElement("span"); st.className = "status s-" + j.status; st.textContent = names[j.status] || j.status;
      a.append(img, mid, st); el.appendChild(a);
    });
  }
  load(); setInterval(load, 3000);
})();
