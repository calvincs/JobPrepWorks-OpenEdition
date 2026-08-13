// Resume print page: "Print / Save as PDF" button. CSP is script-src 'self'
// (no inline handlers), so this is a delegated click listener, same pattern
// as app.js's other data-action handlers.
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-action='print']");
    if (!btn) return;
    window.print();
  });
})();
