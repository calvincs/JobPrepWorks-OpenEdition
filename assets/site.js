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

/* Screenshot viewer.
   Progressive enhancement: every thumbnail is already a plain link to its
   image, so with JS off — or on a browser without <dialog>.showModal — a click
   still opens the picture. When we can do better, we intercept and open it in
   a modal instead.

   Escape, focus trapping, and restoring focus to the thumbnail on close all
   come free from the native dialog; we only add backdrop-click and the button. */
(function () {
  var dlg = document.getElementById('lightbox');
  if (!dlg || typeof dlg.showModal !== 'function') return;

  var img = document.getElementById('lightbox-img');
  var cap = document.getElementById('lightbox-cap');
  var raw = document.getElementById('lightbox-raw');

  function open(link) {
    var thumb = link.querySelector('img');
    var figcap = link.closest('figure') && link.closest('figure').querySelector('figcaption');
    img.src = link.getAttribute('href');
    // The thumbnail's alt already describes the screenshot; reuse it rather
    // than inventing a second description that could drift out of step.
    img.alt = thumb ? thumb.alt : '';
    cap.textContent = figcap ? figcap.textContent : (thumb ? thumb.alt : '');
    raw.href = link.getAttribute('href');
    dlg.showModal();
  }

  document.addEventListener('click', function (e) {
    var link = e.target.closest('.shots a, .step figure a');
    if (!link) return;
    // Let modified clicks through — someone asking for a new tab means it.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    open(link);
  });

  dlg.addEventListener('click', function (e) {
    // The dialog element fills the viewport for hit-testing purposes, so a
    // click landing on it (rather than on a child) is a click on the backdrop.
    if (e.target === dlg || e.target.closest('[data-close-lightbox]')) dlg.close();
  });

  // Drop the image on close so a huge PNG isn't held in memory behind a
  // dialog nobody is looking at.
  dlg.addEventListener('close', function () { img.removeAttribute('src'); });
})();
