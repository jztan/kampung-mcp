// Refreshes a dashboard page in place every data-auto-refresh seconds, so the
// scroll position and any selection survive. No inline script, no eval: the
// page's CSP allows only this same-origin file. The Refresh button stays as
// the no-script fallback.
(function () {
  "use strict";

  var seconds = Number(document.body.dataset.autoRefresh) || 15;
  var parts = ["main.content", ".nav ul", ".updated"];
  var timer = setInterval(tick, seconds * 1000);

  function busy() {
    var el = document.activeElement;
    if (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
      return true;
    }
    var selection = window.getSelection();
    return selection !== null && !selection.isCollapsed;
  }

  function stop() {
    clearInterval(timer);
    var stamp = document.querySelector(".updated");
    if (stamp) {
      stamp.textContent = "Auto-refresh stopped, sign in again";
    }
  }

  function swap(fresh) {
    parts.forEach(function (selector) {
      var next = fresh.querySelector(selector);
      var current = document.querySelector(selector);
      if (next && current) {
        current.replaceWith(document.importNode(next, true));
      }
    });
  }

  function tick() {
    if (document.visibilityState !== "visible" || busy()) {
      return;
    }
    fetch(location.href, { credentials: "same-origin" })
      .then(function (response) {
        var type = response.headers.get("content-type") || "";
        // An expired session redirects to the login page.
        if (response.redirected || response.status !== 200 ||
            type.indexOf("text/html") !== 0) {
          stop();
          return;
        }
        return response.text().then(function (html) {
          var fresh = new DOMParser().parseFromString(html, "text/html");
          if (!fresh.body.hasAttribute("data-auto-refresh")) {
            stop();
            return;
          }
          if (!busy()) {
            swap(fresh);
          }
        });
      })
      .catch(function () {
        // Server unreachable: keep the current page and try again next tick.
      });
  }
})();
