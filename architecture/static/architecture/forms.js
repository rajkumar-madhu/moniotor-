/* Link list and suggestion tables: select-all and a confirm prompt for bulk delete. */
(function () {
  'use strict';
  document.querySelectorAll('[data-check-all]').forEach(function (box) {
    box.addEventListener('change', function () {
      var table = box.closest('table');
      if (!table) return;
      table.querySelectorAll('tbody input[type="checkbox"]').forEach(function (c) { c.checked = box.checked; });
    });
  });
  document.querySelectorAll('[data-confirm-click]').forEach(function (b) {
    b.addEventListener('click', function (e) {
      var any = b.form && b.form.querySelector('input[name="ids"]:checked');
      if (!any) { e.preventDefault(); return; }
      if (!window.confirm(b.dataset.confirmClick)) e.preventDefault();
    });
  });
})();
