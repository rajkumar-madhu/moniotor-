(function () {
  const root = document.getElementById("nw-fabric");
  if (!root) return;
  const url = root.dataset.liveUrl;
  const ms = Number(root.dataset.poll || "30") * 1000;

  function apply(data) {
    if (!data || !data.totals) return;
    const map = {
      "fab-total": data.totals.total,
      "fab-up": data.totals.up,
      "fab-down": data.totals.down,
      "fab-unknown": data.totals.unknown,
    };
    Object.keys(map).forEach((key) => {
      const el = root.querySelector(`[data-live="${key}"]`);
      if (el) el.textContent = String(map[key]);
    });
    (data.nodes || []).forEach((n) => {
      const card = root.querySelector(`.nw-fabric-node[data-pk="${n.pk}"]`);
      if (!card) return;
      card.classList.remove("st-up", "st-down", "st-unknown");
      card.classList.add(`st-${n.status || "unknown"}`);
      const badge = card.querySelector(".nw-fabric-node-badge .badge");
      if (badge) {
        badge.className = `badge ${n.status || "unknown"}`;
        badge.textContent = n.status || "unknown";
      }
    });
  }

  function tick() {
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null))
      .then(apply)
      .catch(() => {});
  }

  tick();
  if (ms > 0) window.setInterval(tick, ms);
})();
