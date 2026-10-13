// SPDX-License-Identifier: Apache-2.0
"use strict";
// The pairing page: exchanges the 8-digit code shown by the portal for this device's token cookie.
(() => {
  const form = document.getElementById("pair-form");
  const input = document.getElementById("pair-code");
  const error = document.getElementById("pair-error");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.hidden = true;
    let res = null;
    try {
      res = await fetch("/api/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: input.value }) });
    } catch {
      res = null;
    }
    if (res && res.ok) {
      location.href = "/";
      return;
    }
    error.textContent = !res ? "The portal could not be reached."
      : res.status === 429 ? "Too many tries. Wait a minute, then try again."
      : "That code is wrong or already used. Check the code the portal shows now.";
    error.hidden = false;
  });
})();
