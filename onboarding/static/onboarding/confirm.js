/* Confirm step: ask the server (POST + CSRF) to check Prometheus for up == 1 every 10 s and update the
   state cells in place. textContent only - nothing from the response is ever parsed as HTML. */
(function () {
  "use strict";
  var panel = document.getElementById("ob-live");
  if (!panel || !panel.getAttribute("data-poll")) { return; }
  var url = panel.getAttribute("data-poll-url");
  var note = document.getElementById("ob-live-note");
  var tokenInput = document.querySelector("input[name=csrfmiddlewaretoken]");
  if (!url || !tokenInput) { return; }
  var tones = ["mute", "info", "warn", "ok", "crit"];
  var timer = null, misses = 0;

  function paint(data) {
    (data.targets || []).forEach(function (t) {
      var row = panel.querySelector('tr[data-target="' + String(t.id).replace(/[^0-9]/g, "") + '"]');
      if (!row) { return; }
      var badge = row.querySelector('[data-field="state"]');
      if (badge) {
        badge.textContent = t.state_label;
        tones.forEach(function (c) { badge.classList.remove(c); });
        if (tones.indexOf(t.tone) >= 0) { badge.classList.add(t.tone); }
      }
      var first = row.querySelector('[data-field="first"]');
      if (first && t.first_sample) { first.textContent = t.first_sample; }
    });
    var s = data.summary || {};
    if (note) {
      note.textContent = s.total ? (s.live + " of " + s.total + " live" + (data.run.status === "complete" ? " - run complete" : "")) : "";
    }
    return data.run && data.run.status === "complete";
  }

  function tick() {
    var body = new FormData();
    body.append("csrfmiddlewaretoken", tokenInput.value);
    fetch(url, { method: "POST", body: body, credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { if (!r.ok) { throw new Error("poll"); } return r.json(); })
      .then(function (data) {
        misses = 0;
        if (paint(data)) { window.location.reload(); return; }
        timer = window.setTimeout(tick, 10000);
      })
      .catch(function () {
        misses += 1;
        if (note) { note.textContent = "Could not reach the server; retrying"; }
        if (misses < 6) { timer = window.setTimeout(tick, 15000); }
      });
  }
  timer = window.setTimeout(tick, 4000);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden && timer) { window.clearTimeout(timer); timer = null; }
    else if (!document.hidden && !timer) { timer = window.setTimeout(tick, 1000); }
  });
})();
