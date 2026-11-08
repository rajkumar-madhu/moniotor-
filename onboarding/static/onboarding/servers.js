/* Copy buttons for the install commands. textContent only: the command is read from the <pre>, never built from HTML. */
(function () {
  document.querySelectorAll('[data-copy-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var pre = document.getElementById(btn.getAttribute('data-copy-target'));
      if (!pre) { return; }
      var text = pre.textContent;
      var done = function () {
        var old = btn.getAttribute('data-label') || btn.textContent;
        btn.setAttribute('data-label', old);
        btn.textContent = 'Copied';
        setTimeout(function () { btn.textContent = old; }, 1600);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { selectAndCopy(pre, done); });
      } else {
        selectAndCopy(pre, done);
      }
    });
  });
  function selectAndCopy(pre, done) {
    var range = document.createRange();
    range.selectNodeContents(pre);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    try { if (document.execCommand('copy')) { done(); } } catch (e) { /* the text stays selected for a manual copy */ }
  }
})();
