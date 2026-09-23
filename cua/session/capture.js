// Spec §7.5: best-effort human-action capture. Re-runs on every document Chromium loads
// (injected via page.add_init_script, which Playwright re-applies to each new document
// automatically -- this script does not wire its own load-event handling for that).
//
// Cannot be the sole audit record: stopPropagation, an uninstrumented iframe, and a
// canvas-based control all defeat it. The bracket (HumanActionBracket, actions.py) is the
// strong half; this is the continuous, unverifiable half.
//
// Password fields never emit their keystrokes: target.type === "password" ? null : target.value
// ensures credentials never reach the capture endpoint in the first place, the strongest form
// of "keep a credential out of evidence."
(function () {
  if (typeof window.__cuaCaptureEndpoint !== "string") {
    return; // no session has told this page where to post -- do nothing
  }

  function post(event) {
    try {
      fetch(window.__cuaCaptureEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(event),
        keepalive: true,
      });
    } catch (e) {
      // Best-effort: a failed post is dropped, never raised into the page it instruments.
    }
  }

  document.addEventListener(
    "click",
    function (e) {
      var target = e.target;
      post({
        type: "click",
        role: target.getAttribute("role") || target.tagName.toLowerCase(),
        name: target.getAttribute("aria-label") || target.textContent || null,
        value: null,
        url: window.location.href,
        timestamp: Date.now(),
      });
    },
    true
  );

  document.addEventListener(
    "input",
    function (e) {
      var target = e.target;
      post({
        type: "input",
        role: target.getAttribute("role") || target.tagName.toLowerCase(),
        name: target.getAttribute("aria-label") || target.name || null,
        value: target.type === "password" ? null : target.value,
        url: window.location.href,
        timestamp: Date.now(),
      });
    },
    true
  );

  window.addEventListener("popstate", function () {
    post({ type: "navigation", role: null, name: null, value: null,
          url: window.location.href, timestamp: Date.now() });
  });
})();
