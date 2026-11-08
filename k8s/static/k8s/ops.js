/* Kubernetes issues: select-all for the bulk "open incident" action. textContent only; no HTML is built. */
(function () {
  'use strict';
  document.querySelectorAll('[data-check-all]').forEach(function (head) {
    var boxes = function () { return document.querySelectorAll('input[name="' + head.getAttribute('data-check-all') + '"]'); };
    var count = document.querySelector('[data-selected-count]');
    function update() {
      var all = boxes(), n = 0;
      all.forEach(function (b) { if (b.checked) n += 1; });
      if (count) count.textContent = String(n);
      document.querySelectorAll('[data-needs-selection]').forEach(function (btn) { btn.disabled = n === 0; });
      head.checked = all.length > 0 && n === all.length;
      head.indeterminate = n > 0 && n < all.length;
    }
    head.addEventListener('change', function () {
      boxes().forEach(function (b) { b.checked = head.checked; });
      update();
    });
    boxes().forEach(function (b) { b.addEventListener('change', update); });
    update();
  });
})();
