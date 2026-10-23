// New job: upload image, live preview, upload source, create job.
(function () {
  const $ = (id) => document.getElementById(id);
  let imageId = null, sourceId = null, sourceKind = null, timer = null;

  // "Print label" only makes sense with artwork and a source; off = burn only
  function updatePrintRow() {
    const both = Boolean(imageId && sourceId);
    $("print-row").hidden = !both; $("print-hint").hidden = !both;
  }

  function setList(el, items) { el.innerHTML = ""; (items || []).forEach((t) => { const li = document.createElement("li"); li.textContent = t; el.appendChild(li); }); }

  function labelOpts() {
    return { fit: document.querySelector('input[name=fit]:checked').value, scale: $("scale").value,
             ox: $("ox").value, oy: $("oy").value, rot: $("rot").value, bg: $("bg").value, hub: $("hub").checked ? 1 : 0,
             media: $("media").value, inner: $("inner").value, outer: $("outer").value };
  }
  function updateLabels() {
    $("scale-v").textContent = Math.round($("scale").value * 100) + "%";
    $("ox-v").textContent = $("ox").value + " mm"; $("oy-v").textContent = $("oy").value + " mm";
    $("rot-v").textContent = $("rot").value + "°";
  }
  // ---- preview: draw in the browser right away, then the exact image from the server ----------
  let localImg = null;                 // the chosen image, decoded by the browser (not for PSD/TIFF)
  let version = 0, inFlight = false, pending = false, checkTimer = null;
  const DISC = 120, HUB = 17, HOLE = 15;
  function ringMm() {
    const opt = $("media").selectedOptions[0];
    if ($("media").value === "custom") return [Number($("inner").value), Number($("outer").value)];
    return [Number(opt.dataset.inner), Number(opt.dataset.outer)];
  }

  function drawLive() {
    const cv = $("preview-live");
    if (!localImg || !localImg.naturalWidth || !imageId) { cv.hidden = true; return; }
    const n = cv.width, o = labelOpts(), w = localImg.naturalWidth, h = localImg.naturalHeight;
    const ctx = cv.getContext("2d");
    ctx.save(); ctx.clearRect(0, 0, n, n);
    ctx.fillStyle = o.bg; ctx.fillRect(0, 0, n, n);
    const f = (o.fit === "cover" ? Math.max : Math.min)(n / w, n / h) * Math.max(Number(o.scale), 0.01);
    const mm = n / DISC;
    ctx.translate(n / 2 + Number(o.ox) * mm, n / 2 + Number(o.oy) * mm);
    ctx.rotate(-Number(o.rot) * Math.PI / 180);                 // positive = counter-clockwise, same as the server
    ctx.drawImage(localImg, -w * f / 2, -h * f / 2, w * f, h * f);
    ctx.restore();
    const ring = (outerMm, innerMm) => { ctx.beginPath(); ctx.arc(n / 2, n / 2, outerMm * mm / 2, 0, 2 * Math.PI);
      ctx.arc(n / 2, n / 2, innerMm * mm / 2, 0, 2 * Math.PI, true); };
    const [inner, outer] = ringMm();
    ctx.globalCompositeOperation = "destination-in"; ring(outer, o.hub ? HUB : inner); ctx.fill();
    ctx.globalCompositeOperation = "destination-over"; ctx.fillStyle = "#c6c8cd"; ring(DISC, HOLE); ctx.fill();
    ctx.globalCompositeOperation = "source-over";
    cv.hidden = false;
  }

  async function fetchServer() {
    if (inFlight) { pending = true; return; }
    inFlight = true;
    const v = version;
    const q = new URLSearchParams({ size: 600, ...labelOpts() });
    if (imageId) q.set("image", imageId);
    try {
      const r = await fetch("/api/preview.png?" + q.toString());
      if (r.ok) {
        const url = URL.createObjectURL(await r.blob());
        const img = $("preview"); const old = img.src;
        img.onload = () => { if (old.startsWith("blob:")) URL.revokeObjectURL(old); if (v === version) $("preview-live").hidden = true; };
        img.src = url;
      }
    } catch (_) { /* the next change tries again */ }
    inFlight = false;
    if (pending) { pending = false; fetchServer(); }
  }

  async function fetchChecks() {
    if (!imageId) { setChecks([]); return; }
    const q = new URLSearchParams({ ...labelOpts(), image: imageId });
    const r = await fetch("/api/label-check?" + q.toString());
    if (r.ok) { const j = await r.json(); setChecks(j.checks); $("art-ppi").textContent = `${j.ppi} ppi on the disc`; }
  }
  function setChecks(items) {
    const ul = $("art-checks"); ul.innerHTML = "";
    (items || []).forEach((c) => {
      const li = document.createElement("li"); li.className = "chk chk-" + c.level;
      const b = document.createElement("b"); b.textContent = c.level === "warning" ? "Warning" : c.level === "error" ? "Error" : "Info";
      li.append(b, " " + c.message); ul.appendChild(li);
    });
    if (imageId && !(items || []).some((c) => c.level !== "info")) {
      const li = document.createElement("li"); li.className = "chk chk-ok"; li.textContent = "No problems found for the printer.";
      ul.prepend(li);
    }
  }

  function refreshPreview() {
    updateLabels();
    version++;
    drawLive();
    clearTimeout(timer);
    timer = setTimeout(fetchServer, 250);
    clearTimeout(checkTimer);
    checkTimer = setTimeout(fetchChecks, 600);
  }

  $("image").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    $("image-info").textContent = "Uploading and checking...";
    localImg = null;
    const li = new Image(); li.onload = () => { localImg = li; drawLive(); }; li.src = URL.createObjectURL(f);
    const fd = new FormData(); fd.append("image", f);
    const r = await fetch("/api/image", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) { $("image-info").textContent = j.error || "Upload failed"; return; }
    imageId = j.id; updatePrintRow();
    showImageInfo(j.name, j.width, j.height, j.info);
    setChecks(j.checks);
    $("label-opts").disabled = false;
    refreshPreview();
  });
  function showImageInfo(name, width, height, inf) {
    inf = inf || {};
    $("image-info").innerHTML = "";
    $("image-info").append(`${name}: ${width} x ${height} px, ${inf.format || "?"}, ${inf.mode || ""}, colour profile: ${inf.profile || "none"} · `);
    const ppi = document.createElement("span"); ppi.id = "art-ppi"; $("image-info").appendChild(ppi);
  }
  ["scale", "ox", "oy", "rot", "bg", "hub", "inner", "outer"].forEach((id) => $(id).addEventListener("input", refreshPreview));
  function updatePsdLink() {
    const q = new URLSearchParams({ media: $("media").value, inner: $("inner").value, outer: $("outer").value });
    $("psd-link").href = "/template.psd?" + q.toString();
  }
  $("media").addEventListener("change", () => { $("media-custom").hidden = $("media").value !== "custom"; updatePsdLink(); refreshPreview(); });
  ["inner", "outer"].forEach((id) => $(id).addEventListener("change", updatePsdLink));
  document.querySelectorAll("input[name=fit]").forEach((el) => el.addEventListener("change", refreshPreview));
  $("reset-label").addEventListener("click", () => {
    $("scale").value = 1; $("ox").value = 0; $("oy").value = 0; $("rot").value = 0; $("bg").value = "#ffffff";
    $("hub").checked = false; document.querySelector('input[name=fit][value=cover]').checked = true; refreshPreview();
  });
  $("clear-image").addEventListener("click", () => {
    imageId = null; updatePrintRow(); localImg = null; $("image").value = ""; $("image-info").textContent = ""; setChecks([]);
    $("label-opts").disabled = true; refreshPreview();
  });

  // ---- source: ISO, BIN/CUE or audio CD -------------------------------------------
  let tracks = [];          // audio CD: {file, info, title, performer, pause, isrc}
  const fmtTime = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const fmtDb = (v, unit) => v == null ? "" : `${v > 0 ? "+" : ""}${v.toFixed(1)} ${unit}`;
  const cleanTitle = (name) => name.replace(/\.[^.]+$/, "").replace(/_/g, " ").replace(/^\d+[\s.-]*/, "").trim();
  // "01 Artist - Title": if every track starts with the same artist, strip it and use it as the album artist
  function splitArtist() {
    const parts = tracks.map((t) => t.title.match(/^(.+?) - (.+)$/));
    if (tracks.length < 2 || parts.some((m) => !m) || new Set(parts.map((m) => m[1])).size !== 1) return;
    tracks.forEach((t, i) => { t.title = parts[i][2]; });
    if (!$("artist").value) $("artist").value = parts[0][1];
  }

  function upload(url, files, bar, done) {
    const fd = new FormData(); files.forEach((f) => fd.append("files", f));
    const xhr = new XMLHttpRequest();
    bar.hidden = false; bar.firstElementChild.style.width = "0";
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.firstElementChild.style.width = (100 * ev.loaded / ev.total) + "%"; };
    xhr.onload = () => { bar.hidden = true; let j = {}; try { j = JSON.parse(xhr.responseText); } catch (_) {} done(xhr.status, j); };
    xhr.onerror = () => { bar.hidden = true; done(0, { error: "Upload failed" }); };
    xhr.open("POST", url); xhr.send(fd);
  }

  function addTracks(list) {
    const pause = Number($("default-pause").value);
    list.forEach((info) => tracks.push({ file: info.file, info, title: cleanTitle(info.file), performer: "", pause, isrc: "" }));
    splitArtist();
    renderTracks();
  }

  function renderTracks() {
    const ol = $("tracks"); ol.innerHTML = "";
    const gapless = $("gapless").checked;
    tracks.forEach((t, i) => {
      const li = document.createElement("li"); li.className = "track";
      const inf = t.info;
      const peak = inf.true_peak_db != null ? ` · true peak ${fmtDb(inf.true_peak_db, "dBTP")}` : "";
      const lufs = inf.loudness_lufs != null ? ` · ${fmtDb(inf.loudness_lufs, "LUFS")}` : "";
      li.innerHTML = `
        <div class="track-head"><span class="track-no">${i + 1}</span>
          <input type="text" class="t-title" maxlength="80" placeholder="Title (CD-TEXT)">
          <button type="button" class="t-up" title="Move up" ${i === 0 ? "disabled" : ""}>&uarr;</button>
          <button type="button" class="t-down" title="Move down" ${i === tracks.length - 1 ? "disabled" : ""}>&darr;</button>
          <button type="button" class="t-del" title="Remove">&times;</button></div>
        <div class="track-meta"></div>
        <ul class="warn"></ul>
        <div class="row track-opts">
          <label>Pause before <input type="number" class="t-pause" min="0" max="10" step="0.5"> s</label>
          <label>Artist (if different) <input type="text" class="t-perf" maxlength="80"></label>
          <label>ISRC <input type="text" class="t-isrc" maxlength="15" placeholder="optional"></label>
        </div>`;
      li.querySelector(".track-meta").textContent =
        `${t.file} · ${fmtTime(inf.duration_s)} · ${inf.description} · ${inf.conversion}${peak}${lufs}`;
      setList(li.querySelector(".warn"), inf.warnings);
      (inf.notes || []).slice().reverse().forEach((text) => {
        const note = document.createElement("p"); note.className = "hint track-note"; note.textContent = text;
        li.querySelector(".warn").after(note);
      });
      const title = li.querySelector(".t-title"); title.value = t.title; title.addEventListener("input", () => { t.title = title.value; });
      const perf = li.querySelector(".t-perf"); perf.value = t.performer; perf.addEventListener("input", () => { t.performer = perf.value; });
      const isrc = li.querySelector(".t-isrc"); isrc.value = t.isrc; isrc.addEventListener("input", () => { t.isrc = isrc.value; });
      const p = li.querySelector(".t-pause");
      if (i === 0) { p.value = 2; p.disabled = true; p.title = "The first track always starts after 2 seconds"; }
      else { p.value = gapless ? 0 : t.pause; p.disabled = gapless; p.addEventListener("input", () => { t.pause = Number(p.value) || 0; updateTotal(); }); }
      li.querySelector(".t-up").addEventListener("click", () => { [tracks[i - 1], tracks[i]] = [tracks[i], tracks[i - 1]]; renderTracks(); });
      li.querySelector(".t-down").addEventListener("click", () => { [tracks[i + 1], tracks[i]] = [tracks[i], tracks[i + 1]]; renderTracks(); });
      li.querySelector(".t-del").addEventListener("click", () => { tracks.splice(i, 1); renderTracks(); });
      ol.appendChild(li);
    });
    updateTotal();
  }

  function audioSeconds() {
    const gapless = $("gapless").checked;
    let s = tracks.length ? 2 : 0;
    tracks.forEach((t, i) => { s += t.info.duration_s + (i > 0 && !gapless ? t.pause : 0); });
    return s;
  }
  function updateTotal() {
    const el = $("audio-total"); if (!tracks.length) { el.innerHTML = '<p class="hint">No tracks yet.</p>'; return; }
    const s = audioSeconds(), max = 80 * 60, pct = Math.min(100, (100 * s) / max);
    let note = "";
    if (s > max) note = '<p class="error">Too long: an 80-minute CD-R is full. Remove a track or shorten the pauses.</p>';
    else if (s > 74 * 60) note = '<p class="hint">Longer than 74 minutes: fits on an 80-minute CD-R, but is above the original CD length.</p>';
    if (tracks.length > 99) note += '<p class="error">An audio CD has at most 99 tracks.</p>';
    const tails = tracks.slice(0, -1).map((t) => t.info.tail_silence_s);
    const extraPause = tracks.slice(1).some((t) => t.pause > 0) && !$("gapless").checked;
    if (tracks.length > 1 && tails.every((v) => v != null && v >= 2) && extraPause) {
      const min = Math.min(...tails).toFixed(1);
      note += `<p class="hint">Your tracks already end with at least ${min} s of silence. An extra pause makes the silence between songs longer.
        <button type="button" id="pause-zero">Set pause to 0 s</button></p>`;
    }
    el.innerHTML = `<div><b>${tracks.length}</b> tracks · <b>${fmtTime(s)}</b> of 80:00 (including pauses)</div>
      <div class="pg-bar"><div style="width:${pct}%;${s > max ? "background:var(--error)" : ""}"></div></div>${note}`;
    const pz = $("pause-zero");
    if (pz) pz.addEventListener("click", () => { $("default-pause").value = "0"; tracks.forEach((t) => { t.pause = 0; }); renderTracks(); });
  }
  $("gapless").addEventListener("change", renderTracks);
  $("default-pause").addEventListener("change", () => { const v = Number($("default-pause").value); tracks.forEach((t) => { t.pause = v; }); renderTracks(); });

  function showSource(kind, files) {
    const kinds = { iso: "Data CD (ISO)", audio: "Build an audio CD", cue: "BIN/CUE image" };
    $("source-info").textContent = kind === "audio" ? kinds.audio : `${kinds[kind] || kind}: ${files.join(", ")}`;
    $("audio-cd").hidden = kind !== "audio"; $("clear-source").hidden = false;
  }

  $("source").addEventListener("change", (e) => {
    const files = Array.from(e.target.files); if (!files.length) return;
    $("source-info").textContent = "Uploading and measuring...";
    upload("/api/source", files, $("upload-progress"), (status, j) => {
      if (status !== 200) { $("source-info").textContent = j.error || "Upload failed"; sourceId = null; updatePrintRow(); return; }
      sourceId = j.id; sourceKind = j.kind; tracks = []; updatePrintRow();
      setList($("source-warn"), j.warnings);
      showSource(j.kind, j.files);
      if (j.kind === "audio") addTracks(j.tracks || []);
    });
  });
  $("add-tracks").addEventListener("change", (e) => {
    const files = Array.from(e.target.files); if (!files.length || !sourceId) return;
    upload(`/api/source/${sourceId}/files`, files, $("add-progress"), (status, j) => {
      $("add-tracks").value = "";
      if (status !== 200) { alert(j.error || "Adding failed"); return; }
      addTracks(j.tracks || []);
    });
  });
  $("clear-source").addEventListener("click", () => {
    sourceId = null; sourceKind = null; tracks = []; updatePrintRow(); $("source").value = ""; $("source-info").textContent = "";
    setList($("source-warn"), []); $("audio-cd").hidden = true; $("clear-source").hidden = true;
  });

  // everything that describes the CD; used for a job and for a release
  function collectBody(errEl) {
    const body = {
      image_id: imageId, source_id: sourceId, label: labelOpts(), title: $("title").value,
      copies: $("copies").value, ribbon: $("ribbon").value, input_bin: $("input_bin").value,
      output_bin: $("output_bin").value, reject_bin: $("reject_bin").value,
      album: $("album").value, artist: $("artist").value,
    };
    if (imageId && sourceId && !$("print").checked) body.print = false;
    if (sourceKind === "audio") {
      if (!tracks.length) { errEl.textContent = "The CD has no tracks."; return null; }
      if (audioSeconds() > 80 * 60) { errEl.textContent = "The CD is too long."; return null; }
      body.tracks = tracks.map((t) => ({ file: t.file, title: t.title, performer: t.performer, pause: t.pause, isrc: t.isrc }));
      body.audio = { gapless: $("gapless").checked, dither: $("dither").value, gain_db: Number($("gain").value),
        speed: $("speed").value, copy_permitted: $("copy").checked, upc: $("upc").value };
    }
    return body;
  }

  // ---- releases: save and open again ----------------------------------------------
  let releaseId = new URLSearchParams(location.search).get("release");
  async function saveRelease(asNew) {
    $("release-status").textContent = "";
    const body = collectBody($("release-status")); if (!body) return;
    body.name = $("release-name").value.trim() || $("title").value.trim() || [$("artist").value, $("album").value].filter(Boolean).join(" - ");
    if (!body.name) { $("release-status").textContent = "Give the release a name."; return; }
    if (releaseId && !asNew) body.release_id = releaseId;
    $("save-release").disabled = true; $("release-status").textContent = "Saving (measuring the tracks)...";
    const r = await fetch("/api/releases", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json(); $("save-release").disabled = false;
    if (!r.ok) { $("release-status").textContent = j.error || "Saving failed"; return; }
    releaseId = j.id; $("release-name").value = j.name;
    history.replaceState(null, "", "/?release=" + encodeURIComponent(j.id));
    $("save-release").textContent = "Update release"; $("save-release-new").hidden = false;
    $("release-status").innerHTML = "";
    $("release-status").append(`Saved as release "${j.name}". `);
    const a = document.createElement("a"); a.href = "/releases"; a.textContent = "Go to releases"; $("release-status").appendChild(a);
  }
  $("save-release").addEventListener("click", () => saveRelease(false));
  $("save-release-new").addEventListener("click", () => saveRelease(true));

  async function openRelease(rid) {
    $("release-status").textContent = "Loading release...";
    const r = await fetch(`/api/releases/${encodeURIComponent(rid)}/open`, { method: "POST" });
    if (!r.ok) { $("release-status").textContent = "Release not found."; releaseId = null; return; }
    const j = await r.json(); const m = j.release;
    $("release-name").value = m.name; $("album").value = m.album || ""; $("artist").value = m.artist || "";
    $("title").value = m.name;
    const L = m.label || {};
    const set = (id, v) => { if (v !== undefined && v !== null) $(id).value = v; };
    set("scale", L.scale); set("ox", L.ox); set("oy", L.oy); set("rot", L.rot); set("bg", L.bg);
    $("hub").checked = String(L.hub) === "1" || L.hub === true;
    document.querySelector(`input[name=fit][value=${L.fit === "contain" ? "contain" : "cover"}]`).checked = true;
    if (L.media) { set("media", L.media); set("inner", L.inner); set("outer", L.outer); $("media-custom").hidden = L.media !== "custom"; }
    updatePsdLink();
    const d = m.job_defaults || {};
    ["ribbon", "input_bin", "output_bin", "reject_bin", "copies"].forEach((k) => { if (d[k] !== undefined && d[k] !== null) set(k, d[k]); });
    if (j.image_id) {
      imageId = j.image_id; updatePrintRow();
      showImageInfo(j.image.name, j.image.width, j.image.height, j.image.info);
      const li = new Image(); li.onload = () => { localImg = li; drawLive(); }; li.src = `/api/image/${imageId}/file`;
      $("label-opts").disabled = false;
    }
    if (j.source_id) {
      sourceId = j.source_id; sourceKind = j.kind; tracks = []; updatePrintRow();
      showSource(j.kind, j.files || []);
      if (j.kind === "audio") {
        const info = Object.fromEntries((m.tracks_info || []).map((t) => [t.file, t]));
        (m.tracks || []).forEach((t) => tracks.push({ file: t.file, info: info[t.file] || { duration_s: 0, description: "", conversion: "", warnings: [] },
          title: t.title, performer: t.performer || "", pause: Number(t.pause), isrc: t.isrc || "" }));
        const A = m.audio || {};
        $("gapless").checked = !!A.gapless; set("dither", A.dither); set("gain", A.gain_db);
        set("speed", A.speed == null ? "max" : A.speed); $("copy").checked = A.copy_permitted !== false; set("upc", A.upc);
        renderTracks();
      }
    }
    $("save-release").textContent = "Update release"; $("save-release-new").hidden = false;
    $("release-status").textContent = `Loaded release "${m.name}". Make changes and choose Update release, or add it to the queue right away.`;
    refreshPreview();
  }

  $("submit").addEventListener("click", async () => {
    $("submit-error").textContent = "";
    const body = collectBody($("submit-error")); if (!body) return;
    $("submit").disabled = true;
    const r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    $("submit").disabled = false;
    if (!r.ok) { $("submit-error").textContent = j.error || "Failed"; return; }
    location.href = "/jobs/" + j.id;
  });
  refreshPreview();
  if (releaseId) openRelease(releaseId);
})();
