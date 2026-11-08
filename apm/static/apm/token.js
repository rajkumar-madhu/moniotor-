/* Copy-to-clipboard for the one-time token page. textContent only. */
(function () {
  'use strict';
  var btn = document.getElementById('apm-copy-token');
  var src = document.getElementById('apm-token');
  var note = document.getElementById('apm-copy-note');
  if (!btn || !src) return;
  btn.addEventListener('click', function () {
    var text = src.textContent.trim();
    function done(ok) { note.textContent = ok ? 'Copied to the clipboard.' : 'Copy failed: select the token and copy it by hand.'; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      var r = document.createRange(); r.selectNodeContents(src);
      var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
      try { done(document.execCommand('copy')); } catch (e) { done(false); }
    }
  });
})();
