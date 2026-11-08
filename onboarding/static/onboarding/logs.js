/* Logs step: while a target is still waiting for its first lines, reload the page every 15 s (a GET, so
   nothing is regenerated and no token is shown) and stop after 40 tries. Never reloads a page that is
   showing a freshly generated configuration. A tab that is hidden when the timer fires is not given up on:
   the timer is re-armed and the page is checked as soon as the tab is visible again. The try counter is kept
   per page address (so one run does not use up another's budget) and is dropped once the lines have arrived. */
(function () {
  "use strict";
  var key = "ob-logs-reloads:" + window.location.pathname;
  var marker = document.querySelector("[data-ob-logs-wait]");
  if (!marker) {
    // nothing left to wait for (lines arrived, or no shipper yet): forget the budget of this page
    try { sessionStorage.removeItem(key); } catch (e) { /* storage unavailable */ }
    return;
  }
  if (document.querySelector("[data-ob-logs-fresh]")) { return; }
  var tries = 0, delay = 15000;
  try { tries = parseInt(sessionStorage.getItem(key) || "0", 10) || 0; } catch (e) { tries = 0; }
  var note = document.getElementById("ob-logs-note");
  if (tries >= 40) { if (note) { note.textContent = "Stopped checking automatically. Reload the page to check again."; } return; }
  var dueAt = Date.now() + delay;
  function reload() {
    try { sessionStorage.setItem(key, String(tries + 1)); } catch (e) { /* storage unavailable: still reload */ }
    window.location.reload();
  }
  function tick() {
    if (document.hidden) { setTimeout(tick, delay); return; }   // re-arm: the tab may come back
    reload();
  }
  setTimeout(tick, delay);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && Date.now() >= dueAt) { reload(); }
  });
  if (note) { note.textContent = "Checking again in 15 s."; }
})();
