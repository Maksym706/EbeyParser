/* EbeyParser dashboard: small progressive enhancements (no frameworks). */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var STATUS_LABELS = {
    new: "Новое", starred: "В избранном", contacted: "Написал продавцу", bought: "Куплено", ignored: "Скрыто"
  };

  // ------------------------------------------------------------------ utils
  function plural(n, one, few, many) {
    n = Math.abs(Math.floor(n));
    if (n % 10 === 1 && n % 100 !== 11) return one;
    if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) return few;
    return many;
  }

  function span(seconds) {
    var minutes = Math.floor(Math.max(0, seconds) / 60);
    if (minutes < 1) return "меньше минуты";
    if (minutes < 60) return minutes + " мин";
    var hours = Math.floor(minutes / 60), rest = minutes % 60;
    if (hours < 24) return rest ? hours + " ч " + rest + " мин" : hours + " ч";
    var days = Math.floor(hours / 24);
    return days + " " + plural(days, "день", "дня", "дней");
  }

  function api(method, url, body) {
    var opts = { method: method, headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var detail = data && data.detail;
          if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg; }).join("; ");
          var err = new Error(detail || ("Ошибка " + res.status));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  // ----------------------------------------------------------------- toasts
  function toast(message, kind, action) {
    var box = $("#toasts");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    var text = document.createElement("span");
    text.textContent = message;
    el.appendChild(text);
    if (action) {
      var btn = document.createElement(action.href ? "a" : "button");
      btn.textContent = action.label;
      if (action.href) { btn.href = action.href; btn.className = "toast-action"; }
      else btn.type = "button";
      btn.addEventListener("click", function () {
        if (action.onClick) action.onClick();
        close();
      });
      el.appendChild(btn);
    }
    box.appendChild(el);
    var timer = setTimeout(close, action ? 7000 : 4000);
    function close() {
      clearTimeout(timer);
      el.classList.add("hide");
      setTimeout(function () { el.remove(); }, 220);
    }
    return close;
  }

  // ------------------------------------------------------------------ theme
  $$("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.dataset.theme === "light" ? "dark" : "light";
      root.dataset.theme = next;
      try { localStorage.setItem("ebp-theme", next); } catch (e) { /* private mode */ }
      var meta = $('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", next === "light" ? "#f3f5f8" : "#0a0e13");
    });
  });

  // ---------------------------------------------------- deal status helpers
  function setStatus(adId, status, note) {
    var body = { status: status };
    if (note !== undefined) body.note = note;
    return api("POST", "/api/deals/" + encodeURIComponent(adId) + "/status", body);
  }

  function applyCardStatus(card, status) {
    card.dataset.status = status;
    card.className = card.className.replace(/\bstatus-\w+/, "status-" + status);
    $$("[data-action]", card).forEach(function (btn) {
      btn.setAttribute("aria-pressed", btn.dataset.action === status ? "true" : "false");
    });
    var ribbon = $("[data-status-ribbon]", card);
    if (ribbon) {
      var show = status === "starred" || status === "bought" || status === "contacted";
      ribbon.hidden = !show;
      ribbon.textContent = show ? STATUS_LABELS[status] : "";
    }
  }

  // quick actions on dashboard cards
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-action]");
    if (!btn) return;
    var card = btn.closest("[data-deal]");
    if (!card) return;
    ev.preventDefault();
    var previous = card.dataset.status || "new";
    var target = btn.dataset.action;
    var next = previous === target ? "new" : target;
    btn.classList.add("is-busy");
    setStatus(card.dataset.deal, next).then(function () {
      applyCardStatus(card, next);
      var undo = { label: "Отменить", onClick: function () {
        setStatus(card.dataset.deal, previous).then(function () { applyCardStatus(card, previous); })
          .catch(function (e) { toast(e.message, "err"); });
      } };
      if (next === "ignored") toast("Объявление скрыто", "", undo);
      else if (next === "bought") toast("Отмечено как купленное — удачной перепродажи!", "ok", undo);
      else if (next === "starred") toast("Добавлено в избранное", "ok");
      else toast("Статус: " + STATUS_LABELS[next]);
    }).catch(function (e) {
      toast(e.message || "Не удалось сохранить", "err");
    }).then(function () { btn.classList.remove("is-busy"); });
  });

  // ------------------------------------------------------------ detail page
  var detail = $("[data-deal-detail]");
  if (detail) {
    var adId = detail.dataset.dealDetail;
    var noteEl = $("[data-note]", detail);
    var current = detail.dataset.status || "new";

    var markStatus = function (status) {
      current = status;
      detail.dataset.status = status;
      $$("[data-set-status]", detail).forEach(function (b) {
        var on = b.dataset.setStatus === status;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      var label = $("[data-status-label]");
      if (label) label.textContent = STATUS_LABELS[status] || status;
    };

    $$("[data-set-status]", detail).forEach(function (b) {
      b.addEventListener("click", function () {
        var status = b.dataset.setStatus;
        b.classList.add("is-busy");
        setStatus(adId, status).then(function () {
          markStatus(status);
          toast("Статус: " + STATUS_LABELS[status], "ok");
        }).catch(function (e) { toast(e.message, "err"); })
          .then(function () { b.classList.remove("is-busy"); });
      });
    });

    var saveNote = $("[data-save-note]", detail);
    if (saveNote && noteEl) {
      var saved = $("[data-note-state]", detail);
      noteEl.addEventListener("input", function () { if (saved) saved.textContent = "Есть несохранённые изменения"; });
      saveNote.addEventListener("click", function () {
        saveNote.classList.add("is-busy");
        setStatus(adId, current, noteEl.value).then(function () {
          if (saved) saved.textContent = "Заметка сохранена";
          toast("Заметка сохранена", "ok");
        }).catch(function (e) { toast(e.message, "err"); })
          .then(function () { saveNote.classList.remove("is-busy"); });
      });
    }
  }

  // gallery: thumbnails switch the main image
  $$("[data-gallery]").forEach(function (gallery) {
    var main = $("[data-gallery-main]", gallery);
    var counter = $("[data-gallery-count]", gallery);
    $$("[data-thumb]", gallery).forEach(function (thumb, i, all) {
      thumb.addEventListener("click", function (ev) {
        ev.preventDefault();
        main.src = thumb.dataset.thumb;
        all.forEach(function (t) { t.classList.toggle("active", t === thumb); });
        if (counter) counter.textContent = (i + 1) + " / " + all.length;
      });
    });
  });

  // ------------------------------------------------------ monitor & "run now"
  var monitorBox = $("[data-monitor]");
  var pollTimer = null;

  function renderPill(pill) {
    if (!monitorBox || !pill) return;
    var el = $("[data-monitor-pill]", monitorBox);
    if (!el) return;
    el.className = "pill pill-" + pill.kind;
    $("[data-pill-text]", el).textContent = pill.text;
  }

  function tickPill() {
    if (!monitorBox || monitorBox.dataset.running === "1") return;
    var next = monitorBox.dataset.nextRun;
    if (!next) return;
    var seconds = (new Date(next).getTime() - Date.now()) / 1000;
    renderPill(seconds <= 60
      ? { kind: "idle", text: "Проверка вот-вот начнётся" }
      : { kind: "idle", text: "Следующая проверка через " + span(seconds) });
  }

  function poll(startedAt) {
    clearTimeout(pollTimer);
    api("GET", "/api/health?ai=0").then(function (data) {
      var m = data.monitor || {};
      monitorBox.dataset.running = m.running ? "1" : "0";
      monitorBox.dataset.nextRun = m.next_run_at || "";
      renderPill(m.pill);
      if (m.running) {
        pollTimer = setTimeout(function () { poll(startedAt); }, 3000);
        return;
      }
      var runBtn = $("[data-run-form] button");
      if (runBtn) runBtn.classList.remove("is-busy");
      var s = m.last_summary;
      if (startedAt && s) {
        var msg = "Проверка завершена: новых " + s.new_listings + ", выгодных " + s.deals_found;
        if (s.errors && s.errors.length) msg += ", ошибок " + s.errors.length;
        toast(msg, s.errors && s.errors.length ? "warn" : "ok", { label: "Обновить", onClick: function () { location.reload(); } });
      }
    }).catch(function () {
      pollTimer = setTimeout(function () { poll(startedAt); }, 6000);
    });
  }

  if (monitorBox) {
    tickPill();
    setInterval(tickPill, 30000);
    if (monitorBox.dataset.running === "1") poll(Date.now());
    var runForm = $("[data-run-form]");
    if (runForm) {
      runForm.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var btn = $("button", runForm);
        btn.classList.add("is-busy");
        api("POST", "/api/run").then(function () {
          monitorBox.dataset.running = "1";
          renderPill({ kind: "running", text: "Идёт проверка…" });
          toast("Проверка запущена — это займёт пару минут", "ok");
          setTimeout(function () { poll(Date.now()); }, 1500);
        }).catch(function (e) {
          btn.classList.remove("is-busy");
          toast(e.message, e.status === 409 ? "warn" : "err");
          if (e.status === 409) poll(Date.now());
        });
      });
    }
  }

  // auction countdowns
  function tickAuctions() {
    $$("[data-ends-at]").forEach(function (el) {
      var seconds = (new Date(el.dataset.endsAt).getTime() - Date.now()) / 1000;
      el.textContent = seconds <= 0 ? "аукцион завершён" : "заканчивается через " + span(seconds);
    });
  }
  if ($("[data-ends-at]")) { tickAuctions(); setInterval(tickAuctions, 30000); }

  // ------------------------------------------------------------- filters
  var filterForm = $("[data-filters]");
  if (filterForm) {
    $$("select", filterForm).forEach(function (sel) {
      sel.addEventListener("change", function () {
        // on phones the panel is collapsible; submit right away on desktop only
        if (window.matchMedia("(min-width: 721px)").matches) {
          if (filterForm.requestSubmit) filterForm.requestSubmit(); else filterForm.submit();
        }
      });
    });
    // drop empty params so URLs stay short
    filterForm.addEventListener("submit", function () {
      $$("input, select", filterForm).forEach(function (el) {
        if (el.name && !el.value) el.disabled = true;
      });
    });
    // back/forward cache would keep them disabled
    window.addEventListener("pageshow", function () {
      $$("input, select", filterForm).forEach(function (el) { el.disabled = false; });
    });
  }

  // ---------------------------------------------------------- notify test
  $$("[data-notify-test]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var out = $("[data-notify-results]");
      btn.classList.add("is-busy");
      if (out) out.innerHTML = "";
      api("POST", "/api/notify/test").then(function (data) {
        var results = (data && data.results) || {};
        var failed = 0;
        Object.keys(results).forEach(function (name) {
          var ok = results[name] === "ok";
          if (!ok) failed++;
          if (out) {
            var row = document.createElement("div");
            row.className = "result " + (ok ? "ok" : "err");
            row.textContent = (ok ? "✓ " : "✗ ") + name + ": " + (ok ? "отправлено" : results[name]);
            out.appendChild(row);
          }
        });
        toast(failed ? "Не все уведомления ушли" : "Тестовое уведомление отправлено", failed ? "warn" : "ok");
      }).catch(function (e) {
        if (out) {
          var row = document.createElement("div");
          row.className = "result err";
          row.textContent = "✗ " + e.message;
          out.appendChild(row);
        }
        toast(e.message, "err");
      }).then(function () { btn.classList.remove("is-busy"); });
    });
  });

  // ------------------------------------------------------------- searches
  var sourceSelect = $("[data-source-select]");
  if (sourceSelect) {
    var syncSource = function () {
      var value = sourceSelect.value;
      $$("[data-source-only]").forEach(function (el) {
        el.hidden = el.dataset.sourceOnly !== value;
      });
    };
    sourceSelect.addEventListener("change", syncSource);
    syncSource();
  }
  $$("[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (!window.confirm(form.dataset.confirm)) ev.preventDefault();
    });
  });
})();
