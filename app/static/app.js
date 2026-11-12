// Confirmation for destructive forms: <form data-confirm="...">
document.addEventListener("submit", (ev) => {
  const msg = ev.target.dataset && ev.target.dataset.confirm;
  if (msg && !window.confirm(msg)) ev.preventDefault();
});

// Click a .copy block to copy its contents.
document.addEventListener("click", async (ev) => {
  const pre = ev.target.closest("pre.copy");
  if (!pre || !navigator.clipboard) return;
  try {
    await navigator.clipboard.writeText(pre.textContent);
    pre.classList.add("copied");
    setTimeout(() => pre.classList.remove("copied"), 1200);
  } catch (_) { /* clipboard unavailable over plain http on some browsers */ }
});

// Live log tail for the run page.
function followLog() {
  const el = document.getElementById("log");
  if (!el) return;
  const runId = el.dataset.run;
  const follow = document.getElementById("follow");
  let offset = -1;
  let wasActive = el.dataset.active === "1";

  async function poll() {
    let data;
    try {
      const r = await fetch(`/runs/${runId}/log?offset=${offset}`, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(r.status);
      data = await r.json();
    } catch (_) {
      setTimeout(poll, 5000);
      return;
    }
    if (data.text) {
      el.textContent += data.text;
      if (follow && follow.checked) el.scrollTop = el.scrollHeight;
    }
    offset = data.offset;
    renderProgress(data.active ? data.progress : null);
    if (data.active) {
      setTimeout(poll, 1500);
    } else if (wasActive) {
      // Finished while we watched: reload once to show final status and stats.
      setTimeout(() => location.reload(), 800);
    }
  }
  poll();
}

// --- progress bar on the run page -------------------------------------------
function fmtBytes(n) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n.toFixed(0)) + " " + units[i];
}

function fmtDuration(s) {
  if (s == null) return "";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${Math.max(1, Math.round(s))} s`;
}

function renderProgress(p) {
  const box = document.getElementById("progress");
  if (!box) return;
  if (!p) { box.hidden = true; return; }
  box.hidden = false;
  const fill = document.getElementById("progress-fill");
  const known = p.pct !== null && p.pct !== undefined;
  box.classList.toggle("indeterminate", !known);
  box.classList.toggle("estimate", !!p.estimate);
  fill.style.width = known ? `${Math.max(1, p.pct).toFixed(1)}%` : "";
  document.getElementById("progress-pct").textContent =
    known ? `${p.estimate ? "~" : ""}${p.pct.toFixed(p.pct < 10 ? 1 : 0)} %` : "";
  document.getElementById("progress-label").textContent =
    (p.label ? p.label + ": " : "") +
    (p.starting ? "starting…" : p.kind === "restic" ? "uploading" : p.estimate || !known ? "scanning and copying" : "copying");

  const parts = [];
  if (p.files_total) {
    parts.push(`${(p.files_checked || 0).toLocaleString()} of ${p.estimate ? "~" : ""}${p.files_total.toLocaleString()} files checked`);
  } else if (p.files_checked != null) {
    parts.push(`${p.files_checked.toLocaleString()} files checked so far`);
  }
  if (p.bytes != null) parts.push(`${fmtBytes(p.bytes)} ${p.bytes_total ? "of " + fmtBytes(p.bytes_total) + " " : ""}${p.kind === "restic" ? "read" : "transferred"}`);
  if (p.speed) parts.push(`${fmtBytes(p.speed)}/s`);
  if (p.elapsed) parts.push(`running ${fmtDuration(p.elapsed)}`);
  if (p.eta) parts.push(`about ${fmtDuration(p.eta)} left`);
  document.getElementById("progress-detail").textContent = parts.join(" · ");
}
