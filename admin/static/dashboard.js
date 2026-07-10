/*
 * admin/static/dashboard.js
 * Shared client logic: theme toggle, CSRF-aware fetch, sidebar, live polling,
 * notifications (badge + optional sound). No external dependencies beyond the
 * CDN-loaded Chart.js used on individual pages.
 */
(function () {
  "use strict";

  // ---- Theme -------------------------------------------------------------
  var THEME_KEY = "mehaat-admin-theme";
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    var icon = document.querySelector("#themeToggle i");
    if (icon) icon.className = t === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
  }
  function initTheme() {
    var saved = localStorage.getItem(THEME_KEY);
    if (!saved) {
      saved = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    applyTheme(saved);
    var btn = document.getElementById("themeToggle");
    if (btn) {
      btn.addEventListener("click", function () {
        var cur = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
        localStorage.setItem(THEME_KEY, cur);
        applyTheme(cur);
        window.dispatchEvent(new CustomEvent("themechange", { detail: cur }));
      });
    }
  }

  // ---- Sidebar (mobile) --------------------------------------------------
  function initSidebar() {
    var toggle = document.getElementById("menuToggle");
    var sidebar = document.getElementById("sidebar");
    var backdrop = document.getElementById("backdrop");
    function close() { sidebar && sidebar.classList.remove("open"); backdrop && backdrop.classList.remove("show"); }
    if (toggle && sidebar) {
      toggle.addEventListener("click", function () {
        sidebar.classList.toggle("open");
        if (backdrop) backdrop.classList.toggle("show");
      });
    }
    if (backdrop) backdrop.addEventListener("click", close);
  }

  // ---- CSRF-aware fetch --------------------------------------------------
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  function apiGet(url) {
    return fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/admin/login"; throw new Error("unauthorized"); }
        return r.json();
      });
  }
  function apiPost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF, "Accept": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  // ---- Notifications -----------------------------------------------------
  var SOUND_KEY = "mehaat-admin-sound";
  var lastUnread = null;
  function soundOn() { return localStorage.getItem(SOUND_KEY) === "1"; }
  function beep() {
    if (!soundOn()) return;
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = 660; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
      o.start(); o.stop(ctx.currentTime + 0.36);
    } catch (e) { /* audio not available */ }
  }
  function renderBadges(unread) {
    document.querySelectorAll("[data-unread-badge]").forEach(function (el) {
      if (unread > 0) { el.textContent = unread; el.style.display = ""; }
      else { el.style.display = "none"; }
    });
    var dot = document.getElementById("notifDot");
    if (dot) dot.style.display = unread > 0 ? "" : "none";
  }
  function pollNotifications() {
    apiGet("/admin/api/notifications").then(function (d) {
      var unread = d.unread || 0;
      if (lastUnread !== null && unread > lastUnread) beep();
      lastUnread = unread;
      renderBadges(unread);
    }).catch(function () {});
  }
  function initNotifications() {
    var toggle = document.getElementById("soundToggle");
    if (toggle) {
      var sync = function () {
        var on = soundOn();
        toggle.classList.toggle("primary", on);
        var i = toggle.querySelector("i");
        if (i) i.className = on ? "bi bi-volume-up" : "bi bi-volume-mute";
      };
      toggle.addEventListener("click", function () {
        localStorage.setItem(SOUND_KEY, soundOn() ? "0" : "1");
        sync();
      });
      sync();
    }
    pollNotifications();
    setInterval(pollNotifications, 10000);
  }

  // ---- Helpers -----------------------------------------------------------
  function timeAgo(iso) {
    if (!iso) return "";
    var then = new Date(iso).getTime();
    if (isNaN(then)) return iso;
    var s = Math.floor((Date.now() - then) / 1000);
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h";
    return Math.floor(s / 86400) + "d";
  }
  function esc(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  // ---- Chart theme colours ----------------------------------------------
  function chartInk() {
    var cs = getComputedStyle(document.documentElement);
    return {
      text: cs.getPropertyValue("--text-secondary").trim() || "#55535f",
      grid: cs.getPropertyValue("--gridline").trim() || "#e6e5ec",
      s1: cs.getPropertyValue("--series-1").trim() || "#2a78d6",
      s2: cs.getPropertyValue("--series-2").trim() || "#1baf7a",
      brand: cs.getPropertyValue("--brand").trim() || "#6d28d9"
    };
  }

  window.MehaatAdmin = {
    apiGet: apiGet, apiPost: apiPost, timeAgo: timeAgo, esc: esc,
    fmtTime: fmtTime, chartInk: chartInk, renderBadges: renderBadges
  };

  document.addEventListener("DOMContentLoaded", function () {
    initTheme();
    initSidebar();
    initNotifications();
  });
})();
