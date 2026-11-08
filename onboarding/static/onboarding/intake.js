/* Show only the SNMP fields that belong to the chosen version / security level. Without JavaScript every
   field stays visible and the server validates the ones that matter. textContent-free: it only toggles hidden. */
(function () {
  "use strict";
  var form = document.querySelector("[data-snmp-form]");
  if (!form) { return; }
  var version = form.querySelector("[name=version]");
  var level = form.querySelector("[name=security_level]");
  function sync() {
    var v3 = version && version.value === "v3";
    var lv = level ? level.value : "authPriv";
    form.querySelectorAll("[data-for]").forEach(function (el) {
      var want = el.getAttribute("data-for");
      var show = true;
      if (want === "v2c") { show = !v3; }
      else if (want === "v3") { show = v3; }
      else if (want === "auth") { show = v3 && lv !== "noAuthNoPriv"; }
      else if (want === "priv") { show = v3 && lv === "authPriv"; }
      el.hidden = !show;
    });
  }
  if (version) { version.addEventListener("change", sync); }
  if (level) { level.addEventListener("change", sync); }
  sync();
})();
