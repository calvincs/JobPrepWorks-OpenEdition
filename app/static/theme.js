// Theme toggle. The blocking head script applies a stored preference before
// first paint; with nothing stored, CSS light-dark() follows the system.
// The account (users.theme) is the cross-device source of truth, so a toggle
// also saves the preference server-side — fire-and-forget: on public pages
// (no session) the request just bounces and the local flip still works.
(function () {
  var btn = document.querySelector(".theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", function () {
    var current = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("jobprep-theme", next); } catch (e) {}
    try {
      fetch("/app/account/display", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "theme=" + next,
        credentials: "same-origin",
      }).catch(function () {});
    } catch (e) {}
  });
})();
