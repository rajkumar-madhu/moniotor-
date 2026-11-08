/* Logs step: while a target is still waiting for its first lines, reload the page every 15 s (a GET, so
   nothing is regenerated and no token is shown) and stop after 40 tries. Never reloads a page that is
   showing a freshly generated configuration, and not while the tab is hidden. */
(function () {
  "use strict";
  var marker = document.querySelector("[data-ob-logs-wait]");
  if (!marker || document.querySelector("[data-ob-logs-fresh]")) { return; }
  var key = "ob-logs-reloads", tries = 0;
  try { tries = parseInt(sessionStorage.getItem(key) || "0", 10) || 0; } catch (e) { tries = 0; }
  var note = document.getElementById("ob-logs-note");
  if (tries >= 40) { if (note) { note.textContent = "Stopped checking automatically. Reload the page to check again."; } return; }
  setTimeout(function () {
    if (document.hidden) { return; }
    try { sessionStorage.setItem(key, String(tries + 1)); } catch (e) { /* storage unavailable: still reload */ }
    window.location.reload();
  }, 15000);
  if (note) { note.textContent = "Checking again in 15 s."; }
})();
