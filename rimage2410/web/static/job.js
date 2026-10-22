// One job: refresh status and log, stop, delete.
(function () {
  const root = document.getElementById("job"); const id = root.dataset.job;
  const names = { queued: "queued", preparing: "preparing", running: "running", done: "done", failed: "failed", stopped: "stopped" };
  const st = document.getElementById("status"), log = document.getElementById("log");
  async function load() {
    const r = await fetch("/api/jobs/" + id); if (!r.ok) return;
    const j = await r.json();
    st.textContent = names[j.status] || j.status; st.className = "status s-" + j.status;
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    if (log.textContent !== j.log) { log.textContent = j.log; if (atEnd) log.scrollTop = log.scrollHeight; }
    window.renderProgress(document.getElementById("progress"), j.progress,
      { onAnswer: (choice) => window.answerJob(id, choice).then(load) });
    document.getElementById("stop").disabled = !["queued", "running"].includes(j.status);
    document.getElementById("delete").disabled = ["queued", "running", "preparing"].includes(j.status);
  }
  document.getElementById("stop").addEventListener("click", async () => { await fetch(`/api/jobs/${id}/stop`, { method: "POST" }); load(); });
  document.getElementById("delete").addEventListener("click", async () => {
    if (!confirm("Delete this job and its files?")) return;
    const r = await fetch("/api/jobs/" + id, { method: "DELETE" });
    if (r.ok) location.href = "/jobs";
  });
  load(); setInterval(load, 1000);
})();
