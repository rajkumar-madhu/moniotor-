/* Get photo from datasheet: the file and the address are alternatives, so choosing one disables the other, and the
 * button says it is working while the PDF is read. Only DOM properties and textContent are written. */
(function () {
  'use strict';
  var f = document.getElementById('ph-find');
  if (!f) return;
  var file = f.querySelector('#pdf_file'), url = f.querySelector('#source_url');
  function sync() {
    var hasFile = !!(file.files && file.files.length);
    url.disabled = hasFile;
    file.disabled = !hasFile && !!url.value.trim();
  }
  file.addEventListener('change', sync);
  url.addEventListener('input', sync);
  sync();
  f.addEventListener('submit', function () {
    var b = f.querySelector('button[type=submit]');
    if (!b) return;
    window.setTimeout(function () {
      b.disabled = true;
      b.textContent = 'Reading\u2026';
    }, 0);
  });
})();
