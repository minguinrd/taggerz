(function () {
  const progress = document.getElementById("progress");
  const PHASE = { scanning: "Scanning", fingerprinting: "Fingerprinting", done: "Done", error: "Error" };

  async function tick() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      if (progress) {
        const failed = data.phase === "error";
        progress.classList.toggle("is-error", failed);
        if (failed) {
          progress.textContent = "Job failed: " + (data.message || "unknown error");
        } else if (data.running) {
          const phase = PHASE[data.phase] || data.phase;
          progress.textContent =
            phase + " — " + data.seen + " seen · " + data.queued + " queued · " +
            data.fingerprinted + " fingerprinted" +
            (data.errors ? " · " + data.errors + " errors" : "");
        } else {
          progress.textContent = "";
        }
      }
      if (data.running) setTimeout(tick, 2000);
      else if (data.phase === "error") setTimeout(tick, 5000);
    } catch (e) {
      if (progress) progress.textContent = "status unavailable";
    }
  }
  tick();

  // Consequential actions (approve/bulk) ask first; all forms give
  // submit feedback by disabling the button while the request is in flight.
  document.addEventListener(
    "submit",
    function (e) {
      const form = e.target;
      const msg = form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) {
        e.preventDefault();
        return;
      }
      const btn = form.querySelector('button[type="submit"]:not([disabled]), button:not([type])');
      if (btn) {
        const label = btn.textContent;
        btn.disabled = true;
        btn.classList.add("is-loading");
        btn.textContent = "Working…";
        setTimeout(function () {
          btn.disabled = false;
          btn.classList.remove("is-loading");
          btn.textContent = label;
        }, 4000);
      }
    },
    true
  );
})();
