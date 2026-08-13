/* Voice-to-text via the Web Speech API (built into Safari/Chrome/Edge).
   Attaches a mic button to every <textarea> and input[data-dictate];
   does nothing in browsers without support. One dictation at a time. */
(function () {
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return;

  var active = null; // { rec, btn }

  function stopActive() {
    if (active) {
      try { active.rec.stop(); } catch (e) { /* already stopped */ }
    }
  }

  function start(field, btn) {
    var rec = new SR();
    rec.lang = navigator.language || "en-US";
    rec.continuous = true;
    rec.interimResults = true;

    // Everything already typed stays; dictation appends after it.
    var base = field.value.replace(/\s+$/, "");
    if (base) base += " ";

    rec.onresult = function (e) {
      var text = "";
      for (var i = 0; i < e.results.length; i++) {
        text += e.results[i][0].transcript;
      }
      field.value = base + text;
      field.dispatchEvent(new Event("input", { bubbles: true }));
    };
    rec.onerror = function (e) {
      btn.title = e.error === "not-allowed"
        ? "Microphone access was blocked - allow it in your browser settings"
        : "Dictation error: " + e.error;
    };
    rec.onend = function () {
      btn.classList.remove("listening");
      btn.setAttribute("aria-pressed", "false");
      if (active && active.rec === rec) active = null;
    };

    try { rec.start(); } catch (e) { return; }
    active = { rec: rec, btn: btn };
    btn.classList.add("listening");
    btn.setAttribute("aria-pressed", "true");
  }

  function attach(field) {
    if (field.dataset.dictateAttached) return;
    field.dataset.dictateAttached = "1";

    var wrap = document.createElement("div");
    wrap.className = "dictate-wrap" + (field.tagName === "INPUT" ? " dictate-input" : "");
    field.parentNode.insertBefore(wrap, field);
    wrap.appendChild(field);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mic-btn";
    btn.title = "Dictate (voice to text)";
    btn.setAttribute("aria-label", "Dictate into this field");
    btn.setAttribute("aria-pressed", "false");
    btn.innerHTML =
      '<svg class="icon" width="14" height="14" aria-hidden="true">' +
      '<use href="/static/icons.svg#mic"/></svg>';
    wrap.appendChild(btn);

    btn.addEventListener("click", function () {
      var mine = active && active.btn === btn;
      stopActive();
      if (!mine) start(field, btn);
    });
  }

  function scan(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("textarea, input[data-dictate]").forEach(attach);
  }

  scan(document);
  document.addEventListener("htmx:afterSwap", function (e) { scan(e.target); });
  // Recording shouldn't outlive the form it was filling.
  document.addEventListener("submit", stopActive, true);
  document.addEventListener("htmx:beforeRequest", stopActive);
})();
