/* Analytics notice with an opt-out.

   Analytics is ON by default for every visitor. This banner tells them so and
   gives them a switch — the choice is remembered in a first-party cookie and
   read back in <head> before gtag.js initialises, because Google's kill switch
   (window['ga-disable-<ID>']) is only honoured if it is already set when the
   tag loads.

   Turning it off also clears the _ga cookies already on the machine. Leaving
   them would mean the visitor said no and still carried the identifier around.

   The banner shows once. After that the footer's "Analytics settings" link
   reopens it, so the decision is reversible in both directions. */
(function () {
  var ID = 'G-7D1G40BK7R';
  var COOKIE = 'jpw_analytics';
  var MAX_AGE = 60 * 60 * 24 * 180; // ~6 months, then we ask again

  function read() {
    var m = document.cookie.match(/(?:^|;\s*)jpw_analytics=([^;]+)/);
    return m ? m[1] : '';
  }

  function save(value) {
    var secure = location.protocol === 'https:' ? ';secure' : '';
    document.cookie = COOKIE + '=' + value + ';path=/;max-age=' + MAX_AGE +
                      ';samesite=lax' + secure;
  }

  function clearAnalyticsCookies() {
    // _ga plus the per-property _ga_<CONTAINER>. Clear on both the exact host
    // and the registrable domain, since GA sets them on the latter.
    var host = location.hostname;
    var domains = ['', host, '.' + host];
    var parts = host.split('.');
    if (parts.length > 2) domains.push('.' + parts.slice(-2).join('.'));
    document.cookie.split(';').forEach(function (raw) {
      var name = raw.split('=')[0].trim();
      if (name.indexOf('_ga') !== 0) return;
      domains.forEach(function (d) {
        document.cookie = name + '=;path=/;max-age=0' + (d ? ';domain=' + d : '');
      });
    });
  }

  function disable() {
    window['ga-disable-' + ID] = true;
    clearAnalyticsCookies();
  }

  var banner = null;
  function close() {
    if (banner) { banner.remove(); banner = null; }
  }

  function show() {
    close();
    var off = read() === 'off';
    banner = document.createElement('section');
    banner.className = 'cookie-banner';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-label', 'Analytics settings');
    banner.tabIndex = -1;
    banner.innerHTML =
      '<div class="cookie-banner-in">' +
        '<p class="cookie-copy"><b>Analytics, not ads.</b> This site uses Google ' +
        'Analytics to see which pages actually help people — anonymous, aggregate, ' +
        'and never sold. It is ' + (off ? '<b>off</b>' : 'on') + ' for you right now. ' +
        'The app you download has no analytics of any kind.</p>' +
        '<div class="cookie-actions">' +
          (off
            ? '<button type="button" class="btn small" data-cc="on">Turn analytics on</button>'
            : '<button type="button" class="btn-ghost small" data-cc="off">Turn analytics off</button>' +
              '<button type="button" class="btn small" data-cc="on">That\'s fine</button>') +
        '</div>' +
      '</div>';
    document.body.appendChild(banner);

    banner.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-cc]');
      if (!btn) return;
      if (btn.getAttribute('data-cc') === 'off') {
        save('off');
        disable();
      } else {
        save('on');
        // Turning it back on takes a reload: gtag.js has already initialised
        // with the flag set, and there is no supported way to un-disable it.
        if (window['ga-disable-' + ID]) { location.reload(); return; }
      }
      close();
    });

    // Move focus to the container rather than a button, so neither choice
    // looks pre-selected. Programmatic focus doesn't trigger :focus-visible.
    banner.focus();
  }

  var choice = read();
  if (choice === 'off') disable();      // belt and braces; <head> already did it
  if (!choice) show();                  // first visit: say what's happening

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-analytics-settings]');
    if (!t) return;
    e.preventDefault();
    show();
  });
})();
