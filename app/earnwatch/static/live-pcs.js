// EarnWatch: Live-Werte der Rechner jede Sekunde nachladen (nur eigene Daten, nur angemeldet).
(function () {
  "use strict";
  var boxes = document.querySelectorAll("[data-pc]");
  if (!boxes.length || !window.fetch) return;
  var busy = false, fails = 0;
  function set(el, key, val) {
    var t = el.querySelectorAll('[data-k="' + key + '"]');
    for (var i = 0; i < t.length; i++) t[i].textContent = val;
  }
  function bar(el, key, pct) {
    var t = el.querySelectorAll('[data-k="' + key + '"]');
    for (var i = 0; i < t.length; i++) {
      t[i].className = "";                                   // feste Breiten-Klasse ablösen
      t[i].style.width = (pct == null ? 0 : Math.max(0, Math.min(100, pct))) + "%";
    }
  }
  function apply(list) {
    for (var i = 0; i < list.length; i++) {
      var pc = list[i];
      var els = document.querySelectorAll('[data-pc="' + pc.id + '"]');
      for (var j = 0; j < els.length; j++) {
        var el = els[j];
        el.classList.toggle("off", !pc.online);
        set(el, "cpu", pc.cpu == null ? "–" : Math.round(pc.cpu) + " %");
        set(el, "mem", pc.mem == null ? "–" : Math.round(pc.mem) + " %");
        set(el, "memtxt", pc.mem_txt);
        set(el, "down", pc.down);
        set(el, "up", pc.up);
        bar(el, "cpubar", pc.cpu);
        bar(el, "membar", pc.mem);
      }
    }
  }
  var detail = document.querySelector("[data-pcdetail]");
  var detailId = detail ? detail.getAttribute("data-pcdetail") : null;
  function applyDetail(d) {
    apply([d]);
    var st = document.querySelector('[data-k="status"]');
    if (st) { st.textContent = d.online ? "online" : "offline"; st.classList.toggle("ok", !!d.online); }
    for (var i = 0; i < d.apps.length; i++) {
      var a = d.apps[i], row = document.querySelector('[data-app="' + a.k + '"]');
      if (!row) continue;
      row.classList.toggle("off", !a.running);
      set(row, "state", a.running ? "läuft" : "nicht gestartet");
      set(row, "cpu", a.cpu == null ? "–" : a.cpu.toFixed(1) + " %");
      set(row, "mem", a.mem);
      set(row, "io", a.io);
      bar(row, "cpubar", a.bar);
    }
  }
  function tick() {
    if (busy || document.hidden) return;
    busy = true;
    var url = detailId && /^[a-f0-9]{12}$/.test(detailId) ? "/api/pcs/" + detailId : "/api/pcs";
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { if (!r.ok) throw r.status; return r.json(); })
      .then(function (d) { fails = 0; if (Array.isArray(d)) apply(d); else applyDetail(d); })
      .catch(function () { fails++; })
      .then(function () { busy = false; });
  }
  setInterval(function () { if (fails < 30) tick(); }, 1000);
})();
