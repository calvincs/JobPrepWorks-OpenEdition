/* Applies the saved theme before first paint (anti-flash). Loaded as a
   blocking script in <head> — it must run before the body renders, so it is
   the one script without `defer`. External (not inline) so the CSP can stay
   at script-src 'self'. */
(function () {
  try {
    var t = localStorage.getItem("jobprep-theme");
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  } catch (e) {}
})();
