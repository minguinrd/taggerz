(function () {
  const progress = document.getElementById("progress");
  async function tick() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      if (progress) {
        progress.textContent = data.running
          ? `${data.phase}: seen ${data.seen}, queued ${data.queued}, fingerprinted ${data.fingerprinted}, errors ${data.errors}`
          : "";
      }
      if (data.running) setTimeout(tick, 2000);
    } catch (e) {
      if (progress) progress.textContent = "status unavailable";
    }
  }
  tick();
})();
