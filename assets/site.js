/* Scroll reveal: .rv blocks fade in, .draw strokes draw themselves.
   Lifted from the JobPrep Studio landing page. Honors prefers-reduced-motion by
   showing everything immediately and binding no observer at all. */
(function () {
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) {
    document.querySelectorAll('.rv, .draw').forEach(function (el) { el.classList.add('in'); });
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      e.target.classList.add('in');
      e.target.querySelectorAll('.draw').forEach(function (d) { d.classList.add('in'); });
      io.unobserve(e.target);
    });
  }, { threshold: 0.2 });
  document.querySelectorAll('.rv').forEach(function (el) { io.observe(el); });
  // The hero's underline draws on load rather than on scroll — it's already
  // in view, and waiting for an intersection that never fires would leave it
  // permanently undrawn.
  requestAnimationFrame(function () {
    var hero = document.querySelector('.hero .draw');
    if (hero) hero.classList.add('in');
  });
})();
