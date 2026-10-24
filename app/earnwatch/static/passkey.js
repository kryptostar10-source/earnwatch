// EarnWatch – Passkey (Face ID / Touch ID). Das einzige Skript der Seite, nur auf Login & Einstellungen.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const isIp = /^[0-9.]+$/.test(location.hostname) || location.hostname.includes(":");
  const supported = !!(window.PublicKeyCredential && navigator.credentials) && !isIp && window.isSecureContext;

  const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));

  async function post(step, body) {
    const r = await fetch("/passkey/" + step, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {}),
    });
    let j = {};
    try { j = await r.json(); } catch (e) { j = { ok: false, error: "Server antwortet nicht richtig." }; }
    if (!j.ok) throw new Error(j.error || "Fehler");
    return j;
  }
  const say = (el, msg, bad) => { if (el) { el.textContent = msg; el.classList.toggle("errtext", !!bad); } };
  const nice = (e) => (e && e.name === "NotAllowedError") ? "Abgebrochen oder Zeit abgelaufen." : (e && e.message) || "Fehler";

  // --- Login -------------------------------------------------------------
  const lbox = $("pk-login");
  if (lbox && supported) {
    lbox.hidden = false;
    $("pk-login-btn").addEventListener("click", async () => {
      const msg = $("pk-login-msg");
      say(msg, "Warte auf Face ID …");
      try {
        const { options } = await post("login-options");
        const cred = await navigator.credentials.get({ publicKey: {
          challenge: unb64u(options.challenge), rpId: options.rpId, timeout: options.timeout,
          userVerification: options.userVerification, allowCredentials: [] } });
        const r = cred.response;
        const res = await post("login-finish", { id: cred.id, response: {
          clientDataJSON: b64u(r.clientDataJSON), authenticatorData: b64u(r.authenticatorData),
          signature: b64u(r.signature), userHandle: r.userHandle ? b64u(r.userHandle) : null } });
        say(msg, "Angemeldet ✓");
        location.href = res.next || "/";
      } catch (e) { say(msg, nice(e), true); }
    });
  }

  // --- Passkey hinzufügen (Einstellungen) ----------------------------------
  const rbox = $("pk-reg");
  const nojs = $("pk-nojs");
  if (rbox && supported) {
    rbox.hidden = false;
    if (nojs) nojs.hidden = true;
    $("pk-reg-btn").addEventListener("click", async () => {
      const msg = $("pk-reg-msg");
      say(msg, "Bitte bestätigen …");
      try {
        const { options: o } = await post("register-options", { password: $("pk-pw").value });
        const cred = await navigator.credentials.create({ publicKey: {
          challenge: unb64u(o.challenge), rp: o.rp,
          user: { id: unb64u(o.user.id), name: o.user.name, displayName: o.user.displayName },
          pubKeyCredParams: o.pubKeyCredParams, timeout: o.timeout, attestation: o.attestation,
          authenticatorSelection: o.authenticatorSelection,
          excludeCredentials: o.excludeCredentials.map((c) => ({ type: c.type, id: unb64u(c.id) })) } });
        await post("register-finish", { name: $("pk-name").value, response: {
          clientDataJSON: b64u(cred.response.clientDataJSON),
          attestationObject: b64u(cred.response.attestationObject) } });
        location.reload();
      } catch (e) { say(msg, nice(e), true); }
    });
  }
})();
