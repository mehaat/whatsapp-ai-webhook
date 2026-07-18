/* ==========================================================================
   support_console.js — v10.2 real-time WhatsApp Support Console
   Polling-based live console (every ~3s). No external deps. Talks to the
   /admin/support/api/* endpoints. Safe for normal browser deployment (uses
   localStorage for UI prefs; all state-changing calls send the CSRF token).
   ========================================================================== */
(function () {
  "use strict";

  var CFG = window.SC_CONFIG || {};
  var API = (CFG.base || "/admin/support") + "/api";
  var POLL = CFG.pollMs || 3000;

  var state = {
    activeWa: null,
    convs: [],
    prev: {},              // wa -> {last_message_at, unread} for notification diffing
    attach: null,          // pending File
    soundOn: load("sc_sound", "1") === "1",
    filterUnread: false,
    search: "",
    lastThreadKey: "",
    firstLoad: true
  };

  /* ---- tiny helpers ---------------------------------------------------- */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function load(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function initials(name, num) { name = (name || "").trim(); if (name) return name.slice(0, 2).toUpperCase(); return (num || "?").slice(-2); }

  function getJSON(url) {
    return fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.json(); });
  }
  function send(url, body, isForm) {
    var opts = { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": CFG.csrf } };
    if (isForm) { opts.body = body; }
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body || {}); }
    return fetch(url, opts).then(function (r) { return r.json().catch(function () { return { ok: false }; }); });
  }

  /* ---- time / date formatting ----------------------------------------- */
  function parseTs(s) { if (!s) return null; var d = new Date(s); return isNaN(d) ? null : d; }
  function fmtTime(s) { var d = parseTs(s); return d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""; }
  function fmtDayLabel(s) {
    var d = parseTs(s); if (!d) return "";
    var today = new Date(); var y = new Date(); y.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  }
  function fmtRel(s) {
    var d = parseTs(s); if (!d) return "";
    var mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return "now"; if (mins < 60) return mins + "m";
    if (mins < 1440) return Math.floor(mins / 60) + "h";
    return fmtDayLabel(s);
  }

  /* ---- ticks ----------------------------------------------------------- */
  function tick(status) {
    switch (status) {
      case "read": return '<span class="sc-tick read">✓✓</span>';
      case "delivered": return '<span class="sc-tick">✓✓</span>';
      case "sent": return '<span class="sc-tick">✓</span>';
      case "failed": return '<span class="sc-tick" style="color:#e5533c">!</span>';
      default: return '<span class="sc-tick">🕓</span>';
    }
  }

  /* ---- INBOX ----------------------------------------------------------- */
  function pollInbox() {
    var q = encodeURIComponent(state.search || "");
    var url = API + "/inbox?q=" + q + (state.filterUnread ? "&unread=1" : "");
    getJSON(url).then(function (res) {
      if (!res || !res.ok) return;
      detectNotifications(res.conversations || []);
      state.convs = res.conversations || [];
      renderInbox();
      updateNavBadge();
    }).catch(function () {});
  }

  function renderInbox() {
    var list = $("#sc-conv-list");
    if (!list) return;
    if (!state.convs.length) { list.innerHTML = '<div class="sc-empty" style="height:auto;padding:30px">No conversations yet.</div>'; return; }
    list.innerHTML = "";
    state.convs.forEach(function (c) {
      var row = el("div", "sc-conv" + (c.wa_number === state.activeWa ? " active" : ""));
      row.setAttribute("data-wa", c.wa_number);
      var badges = "";
      if (c.unread_count > 0) badges += '<span class="sc-unread">' + c.unread_count + '</span>';
      badges += c.ai_enabled ? '<span class="sc-pill sc-pill--ai">AI</span>' : '<span class="sc-pill sc-pill--manual">Manual</span>';
      if (c.assigned_to) badges += '<span class="sc-pill sc-pill--assigned">@' + esc(c.assigned_to) + '</span>';
      row.innerHTML =
        '<div class="sc-avatar">' + esc(initials(c.profile_name, c.wa_number)) + '</div>' +
        '<div class="sc-conv-body">' +
          '<div class="sc-conv-top"><span class="sc-conv-name">' + esc(c.profile_name || c.wa_number) + '</span>' +
          '<span class="sc-conv-time">' + fmtRel(c.last_message_at) + '</span></div>' +
          '<div class="sc-conv-bottom"><span class="sc-conv-msg">' +
          (c.last_direction === "out" ? '↩ ' : '') + esc(c.last_message || "") + '</span>' +
          '<span class="sc-badges">' + badges + '</span></div>' +
        '</div>';
      row.addEventListener("click", function () { openConversation(c.wa_number); });
      list.appendChild(row);
    });
  }

  function detectNotifications(convs) {
    if (state.firstLoad) { state.firstLoad = false; convs.forEach(function (c) { state.prev[c.wa_number] = snapshot(c); }); return; }
    convs.forEach(function (c) {
      var p = state.prev[c.wa_number];
      var isNewInbound = c.last_direction === "in" && (!p || c.last_message_at > (p.last_message_at || ""));
      if (isNewInbound && c.wa_number !== state.activeWa) {
        notify(c.profile_name || c.wa_number, c.last_message || "New message");
      }
      state.prev[c.wa_number] = snapshot(c);
    });
  }
  function snapshot(c) { return { last_message_at: c.last_message_at, unread: c.unread_count }; }

  /* ---- NOTIFICATIONS --------------------------------------------------- */
  function notify(title, body) {
    if (state.soundOn) { try { var a = $("#sc-ping"); if (a) { a.currentTime = 0; a.play().catch(function () {}); } } catch (e) {} }
    if (window.Notification && Notification.permission === "granted") {
      try { new Notification("💬 " + title, { body: body, tag: title }); } catch (e) {}
    }
    document.title = "🔴 New message · Support Console";
  }
  function updateNavBadge() {
    var total = state.convs.reduce(function (n, c) { return n + (c.unread_count || 0); }, 0);
    var badge = document.querySelector("[data-support-unread]");
    if (badge) { badge.textContent = total; badge.style.display = total > 0 ? "" : "none"; }
    if (!total) document.title = "Support Console · ME-HAAT Fashion AI";
  }

  /* ---- STATS ----------------------------------------------------------- */
  function pollStats() {
    getJSON(API + "/stats").then(function (res) {
      if (!res || !res.ok) return;
      Object.keys(res.stats).forEach(function (k) {
        var node = document.querySelector('[data-stat="' + k + '"]');
        if (node) node.textContent = res.stats[k];
      });
    }).catch(function () {});
  }

  /* ---- CONVERSATION / THREAD ------------------------------------------ */
  function openConversation(wa) {
    state.activeWa = wa;
    state.lastThreadKey = "";
    $("#sc-chat-empty").style.display = "none";
    $("#sc-chat").style.display = "flex";
    var c = state.convs.filter(function (x) { return x.wa_number === wa; })[0] || {};
    $("#sc-chat-name").textContent = c.profile_name || wa;
    $("#sc-chat-number").textContent = wa;
    $("#sc-chat-avatar").textContent = initials(c.profile_name, wa);
    renderInbox();
    pollThread(true);
    loadProfile();
    loadNotes();
    document.getElementById("sc-root").classList.remove("sc-show-inbox");
  }

  function pollThread(force) {
    if (!state.activeWa) return;
    getJSON(API + "/thread/" + encodeURIComponent(state.activeWa) + "?mark_read=1").then(function (res) {
      if (!res || !res.ok) return;
      var key = JSON.stringify(res.messages.map(function (m) { return m.id + ":" + (m.status || ""); }));
      if (!force && key === state.lastThreadKey) return;
      state.lastThreadKey = key;
      renderThread(res.messages);
    }).catch(function () {});
  }

  function renderThread(msgs) {
    var box = $("#sc-messages");
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    box.innerHTML = "";
    var lastDay = "";
    msgs.forEach(function (m) {
      var day = fmtDayLabel(m.created_at);
      if (day && day !== lastDay) { box.appendChild(el("div", "sc-day", esc(day))); lastDay = day; }
      var side = m.direction === "in" ? "in" : "out";
      var b = el("div", "sc-bubble " + side);
      var inner = "";
      if (m.source === "admin" && m.admin_user) inner += '<div class="sc-sender">' + esc(m.admin_user) + '</div>';
      if (m.type && m.type !== "text") {
        var icon = m.type === "image" ? "image" : (m.type === "audio" ? "mic" : "file-earmark-pdf");
        inner += '<div class="sc-attach"><i class="bi bi-' + icon + '"></i> ' + esc(m.filename || m.type) + '</div>';
      }
      if (m.text) inner += esc(m.text);
      inner += '<div class="sc-meta">' + fmtTime(m.created_at) + (side === "out" ? " " + tick(m.status) : "") + '</div>';
      b.innerHTML = inner;
      box.appendChild(b);
    });
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  /* ---- PROFILE + NOTES ------------------------------------------------- */
  function loadProfile() {
    getJSON(API + "/profile/" + encodeURIComponent(state.activeWa)).then(function (res) {
      if (!res || !res.ok) return;
      var p = res.profile;
      $("#sc-prof-avatar").textContent = initials(p.profile_name, p.wa_number);
      $("#sc-prof-name").textContent = p.profile_name || p.wa_number;
      $("#sc-prof-number").textContent = p.wa_number;
      setProf("order_count", p.order_count);
      setProf("total_spend", (p.total_spend || 0).toLocaleString());
      setProf("message_count", p.message_count);
      setProf("conversation_count", p.conversation_count);
      setProf("language", p.language || "—");
      setProf("email", p.email || "—");
      setProf("last_order", p.last_order ? (p.last_order.order_name || "—") : "—");
      // AI toggle
      var t = $("#sc-ai-toggle"); t.classList.toggle("on", !!p.ai_enabled);
      $("#sc-ai-label").textContent = p.ai_enabled ? "AI" : "Manual";
      $("#sc-manual-banner").classList.toggle("show", !p.ai_enabled);
      $("#sc-status").value = p.status || "open";
      buildAssignSelect(p.assigned_to);
    }).catch(function () {});
  }
  function setProf(k, v) { var n = document.querySelector('[data-prof="' + k + '"]'); if (n) n.textContent = v; }

  function buildAssignSelect(current) {
    var sel = $("#sc-assign");
    var opts = '<option value="">Unassigned</option><option value="__me__">Assign to me (' + esc(CFG.me) + ')</option>';
    if (current && current !== CFG.me) opts += '<option value="' + esc(current) + '" selected>@' + esc(current) + '</option>';
    sel.innerHTML = opts;
    if (current === CFG.me) sel.value = "__me__";
  }

  function loadNotes() {
    getJSON(API + "/notes/" + encodeURIComponent(state.activeWa)).then(function (res) {
      if (!res || !res.ok) return;
      var box = $("#sc-notes"); box.innerHTML = "";
      if (!res.notes.length) { box.innerHTML = '<div style="color:var(--sc-muted);font-size:12px">No notes yet.</div>'; return; }
      res.notes.forEach(function (n) {
        box.appendChild(el("div", "sc-note-item", esc(n.note) + '<small>' + esc(n.admin_user) + ' · ' + fmtRel(n.created_at) + '</small>'));
      });
    }).catch(function () {});
  }

  /* ---- SEND ------------------------------------------------------------ */
  function doSend() {
    if (!state.activeWa) return;
    var input = $("#sc-text");
    var text = input.value.trim();
    if (state.attach) return sendMedia(text);
    if (!text) return;
    input.value = ""; autoGrow(input);
    send(API + "/send/" + encodeURIComponent(state.activeWa), { text: text }).then(function (res) {
      if (!res.ok) toast("Send failed: " + (res.error || "WhatsApp error"), true);
      pollThread(true);
    });
  }
  function sendMedia(caption) {
    var fd = new FormData();
    fd.append("file", state.attach);
    if (caption) fd.append("caption", caption);
    $("#sc-text").value = "";
    clearAttach();
    send(API + "/send/" + encodeURIComponent(state.activeWa), fd, true).then(function (res) {
      if (!res.ok) toast("Media send failed: " + (res.error || ""), true);
      pollThread(true);
    });
  }

  /* ---- ACTIONS: ai toggle / assign / status / notes ------------------- */
  function toggleAI() {
    if (!state.activeWa) return;
    var on = !$("#sc-ai-toggle").classList.contains("on");
    send(API + "/ai-toggle/" + encodeURIComponent(state.activeWa), { ai_enabled: on }).then(function (res) {
      if (res.ok) { loadProfile(); pollInbox(); }
    });
  }
  function changeAssign() {
    send(API + "/assign/" + encodeURIComponent(state.activeWa), { assigned_to: $("#sc-assign").value }).then(pollInbox);
  }
  function changeStatus() {
    send(API + "/status/" + encodeURIComponent(state.activeWa), { status: $("#sc-status").value });
  }
  function addNote() {
    var t = $("#sc-note-text"); var note = t.value.trim(); if (!note) return;
    send(API + "/notes/" + encodeURIComponent(state.activeWa), { note: note }).then(function (res) {
      if (res.ok) { t.value = ""; loadNotes(); }
    });
  }

  /* ---- SHOPIFY + PAYMENT ---------------------------------------------- */
  function shopifySearch() {
    var q = $("#sc-shopify-q").value.trim(); if (!q) return;
    getJSON(API + "/shopify/search?q=" + encodeURIComponent(q)).then(function (res) {
      var box = $("#sc-shopify-results"); box.innerHTML = "";
      (res.products || []).forEach(function (p) {
        box.appendChild(el("div", "sc-product",
          (p.image ? '<img src="' + esc(p.image) + '">' : '<i class="bi bi-bag"></i>') +
          '<div style="flex:1"><b>' + esc(p.title) + '</b><br>' + esc(p.price || p.formatted_price || "") + '</div>'));
      });
      if (!(res.products || []).length) box.innerHTML = '<div style="color:var(--sc-muted);font-size:12px">No products.</div>';
    });
  }
  function shopifySendCards() {
    var q = $("#sc-shopify-q").value.trim(); if (!q || !state.activeWa) return;
    send(API + "/shopify/send-card/" + encodeURIComponent(state.activeWa), { query: q }).then(function (res) {
      toast(res.ok ? ("Sent " + res.sent + " product card(s)") : ("Failed: " + (res.error || "")), !res.ok);
      pollThread(true);
    });
  }
  function generatePayment() {
    var amt = parseFloat($("#sc-pay-amount").value);
    if (!amt || amt <= 0) return toast("Enter a valid amount", true);
    var cur = $("#sc-pay-currency").value || "INR";
    send(API + "/payment/link/" + encodeURIComponent(state.activeWa), { amount: amt, currency: cur, send: true }).then(function (res) {
      if (res.ok) {
        $("#sc-pay-result").innerHTML = 'Link: <a href="' + esc(res.url) + '" target="_blank">' + esc(res.url) + '</a> ' +
          '<button class="sc-btn sc-btn--ghost" onclick="navigator.clipboard.writeText(\'' + esc(res.url) + '\')">Copy</button>' +
          (res.sent ? ' <span style="color:var(--sc-accent)">✓ sent</span>' : '');
        pollThread(true);
      } else { toast("Payment link failed: " + (res.error || ""), true); }
    });
  }

  /* ---- EMOJI + ATTACH + COMPOSER -------------------------------------- */
  var EMOJIS = "😀 😁 😂 🤣 😊 😍 😘 👍 🙏 🔥 🎉 ✅ ❤️ 🛍️ 👗 👚 👠 💯 🙌 ⭐ 📦 🚚 💳 🤝 👋".split(" ");
  function buildEmoji() {
    var tray = $("#sc-emoji-tray");
    EMOJIS.forEach(function (e) {
      var s = el("span", "sc-emoji", e);
      s.addEventListener("click", function () { var i = $("#sc-text"); i.value += e; i.focus(); autoGrow(i); });
      tray.appendChild(s);
    });
  }
  function autoGrow(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 120) + "px"; }
  function clearAttach() { state.attach = null; $("#sc-attach-preview").classList.remove("show"); $("#sc-file").value = ""; }

  function toast(msg, isErr) {
    var t = el("div", "", esc(msg));
    t.style.cssText = "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:9999;" +
      "padding:10px 18px;border-radius:8px;color:#fff;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.25);" +
      "background:" + (isErr ? "#e5533c" : "#00a884");
    document.body.appendChild(t);
    setTimeout(function () { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(function () { t.remove(); }, 300); }, 2600);
  }

  /* ---- THEME + SOUND --------------------------------------------------- */
  function applyTheme(theme) { $("#sc-root").setAttribute("data-console-theme", theme); save("sc_theme", theme);
    $("#sc-theme-toggle").innerHTML = theme === "dark" ? '<i class="bi bi-sun"></i>' : '<i class="bi bi-moon-stars"></i>'; }

  /* ---- WIRE UP --------------------------------------------------------- */
  function bind() {
    $("#sc-send").addEventListener("click", doSend);
    $("#sc-text").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
    });
    $("#sc-text").addEventListener("input", function () { autoGrow(this); });
    $("#sc-search").addEventListener("input", function () { state.search = this.value.trim(); pollInbox(); });
    $("#sc-filter-unread").addEventListener("click", function () { state.filterUnread = !state.filterUnread; this.classList.toggle("active"); pollInbox(); });
    $("#sc-ai-toggle").addEventListener("click", toggleAI);
    $("#sc-assign").addEventListener("change", changeAssign);
    $("#sc-status").addEventListener("change", changeStatus);
    $("#sc-note-add").addEventListener("click", addNote);
    $("#sc-emoji-btn").addEventListener("click", function () { $("#sc-emoji-tray").classList.toggle("open"); });
    $("#sc-attach-btn").addEventListener("click", function () { $("#sc-file").click(); });
    $("#sc-file").addEventListener("change", function () {
      if (this.files && this.files[0]) { state.attach = this.files[0]; $("#sc-attach-name").textContent = this.files[0].name; $("#sc-attach-preview").classList.add("show"); }
    });
    $("#sc-attach-clear").addEventListener("click", clearAttach);
    $("#sc-shopify-btn").addEventListener("click", function () { togglePanel("#sc-shopify-panel"); });
    $("#sc-pay-btn").addEventListener("click", function () { togglePanel("#sc-pay-panel"); });
    $("#sc-shopify-q").addEventListener("keydown", function (e) { if (e.key === "Enter") shopifySearch(); });
    $("#sc-shopify-send").addEventListener("click", shopifySendCards);
    $("#sc-pay-generate").addEventListener("click", generatePayment);
    $("#sc-theme-toggle").addEventListener("click", function () {
      applyTheme($("#sc-root").getAttribute("data-console-theme") === "dark" ? "light" : "dark");
    });
    $("#sc-sound-toggle").addEventListener("click", function () {
      state.soundOn = !state.soundOn; save("sc_sound", state.soundOn ? "1" : "0");
      this.innerHTML = state.soundOn ? '<i class="bi bi-volume-up"></i>' : '<i class="bi bi-volume-mute"></i>';
    });
    var back = $("#sc-back"); if (back) back.addEventListener("click", function () { document.getElementById("sc-root").classList.add("sc-show-inbox"); });
    document.addEventListener("click", function () { document.title = document.title.replace("🔴 New message · ", ""); }, { once: false });
  }
  function togglePanel(sel) { var p = $(sel); p.style.display = p.style.display === "none" ? "block" : "none"; }

  /* ---- INIT ------------------------------------------------------------ */
  function init() {
    applyTheme(load("sc_theme", "light"));
    $("#sc-sound-toggle").innerHTML = state.soundOn ? '<i class="bi bi-volume-up"></i>' : '<i class="bi bi-volume-mute"></i>';
    buildEmoji();
    bind();
    if (window.Notification && Notification.permission === "default") { try { Notification.requestPermission(); } catch (e) {} }
    pollInbox(); pollStats();
    setInterval(function () { pollInbox(); pollStats(); if (state.activeWa) pollThread(false); }, POLL);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
