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
    if (data.active) {
      setTimeout(poll, 1500);
    } else if (wasActive) {
      // Finished while we watched: reload once to show final status and stats.
      setTimeout(() => location.reload(), 800);
    }
  }
  poll();
}
