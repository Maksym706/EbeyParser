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

  // ------------------------------------------------ dismissible hint banner
  $$("[data-dismiss-hint]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      try { localStorage.setItem("ebp-hide-cat-hint", "1"); } catch (e) { /* private mode */ }
      document.documentElement.classList.add("hide-cat-hint");
    });
  });

  // ---------------------------------------------------------- setup wizard
  var setupForm = $("[data-setup]");
  if (setupForm) {
    $$("[data-js-only]", setupForm).forEach(function (el) { el.hidden = false; });
    var countEl = $("[data-setup-count]", setupForm);
    var estimateEl = $("[data-estimate]", setupForm);
    var num = function (el) { var v = parseFloat(String(el && el.value || "").replace(",", ".")); return isFinite(v) ? v : 0; };
    var updateSetup = function () {
      var cats = $$('input[name="category"]:checked', setupForm).length;
      var wishes = $$('input[name="wish_item"]', setupForm).filter(function (i) { return i.value.trim(); }).length;
      if (countEl) {
        countEl.textContent = (cats || wishes)
          ? "Будет создано: " + cats + " " + plural(cats, "поиск", "поиска", "поисков") + " по категориям" +
            (wishes ? " и " + wishes + " «для себя»" : "")
          : "Выбери хотя бы одну категорию или добавь вещь «для себя»";
      }
      if (estimateEl) {  // same formula as RequestEstimate / suggest_interval in scraper/categories.py
        var d = estimateEl.dataset;
        var cap = parseFloat(d.cap) || 0;
        var pagesRun = cats * parseFloat(d.scanPages) + wishes * parseFloat(d.kwPages);
        var steps = [10, 15, 20, 30, 45, 60, 90, 120];
        var wanted = Math.max(10, cap ? pagesRun * 60 / (cap * parseFloat(d.share)) : 2 * (cats + wishes));
        var suggested = steps.filter(function (s) { return s >= wanted - 1e-9; })[0] || Math.ceil(wanted / 30) * 30;
        var interval = Math.max(1, num($('[name="interval_minutes"]', setupForm)) || suggested);
        var pagesHour = Math.round(pagesRun * 60 / interval);
        var raw = Math.round((pagesRun + (pagesRun ? parseFloat(d.extra) : 0)) * 60 / interval);
        var perHour = cap ? Math.max(pagesHour, Math.min(raw, cap)) : raw;
        var tight = cap && pagesHour > cap / 2;
        var parts = [];
        if (cats) parts.push(cats + " " + plural(cats, "категория", "категории", "категорий") + " × до 3 стр.");
        if (wishes) parts.push(wishes + " " + plural(wishes, "поиск", "поиска", "поисков") + " по словам");
        var text = "Раз в " + interval + " мин: до " + pagesHour + " стр. выдачи в час" +
          (parts.length ? " (" + parts.join(", ") + ")" : "") + ", всего с оценкой объявлений — не больше " + perHour +
          " запросов в час (~" + perHour * 24 + " в сутки).";
        if (cap) {
          text += " На оценку объявлений (страница объявления + 2–3 стр. цен аналогов) остаётся ~" +
            Math.max(0, cap - pagesHour) + " в час из лимита " + cap + ".";
        }
        if (tight) text += " ⚠ Слишком часто: на оценку объявлений почти не останется запросов — поставь раз в " + suggested + " мин.";
        var textEl = $("[data-estimate-text]", estimateEl);
        if (textEl) textEl.textContent = text;
        var sugEl = $("[data-suggested]", setupForm);
        if (sugEl) sugEl.textContent = suggested;
        estimateEl.classList.toggle("warn", !!tight);
      }
    };
    var syncPurpose = function () {
      var checked = $('input[name="purpose"]:checked', setupForm);
      var value = checked ? checked.value : "resale";
      $$("[data-purpose-only]", setupForm).forEach(function (el) { el.hidden = el.dataset.purposeOnly !== value; });
    };
    setupForm.addEventListener("change", function () { updateSetup(); syncPurpose(); });
    setupForm.addEventListener("input", updateSetup);
    setupForm.addEventListener("click", function (ev) {
      var sel = ev.target.closest("[data-cat-select]");
      if (sel) {
        $$('input[name="category"]', setupForm).forEach(function (box) {
          box.checked = sel.dataset.catSelect === "recommended" ? box.dataset.recommended === "1" : false;
        });
        updateSetup();
        return;
      }
      if (ev.target.closest("[data-wish-add]")) {
        var tpl = $("[data-wish-template]", setupForm);
        var list = $("[data-wish-list]", setupForm);
        if (tpl && list) {
          list.appendChild(tpl.content.cloneNode(true));
          var items = $$('input[name="wish_item"]', list);
          items[items.length - 1].focus();
        }
        return;
      }
      var rm = ev.target.closest("[data-wish-remove]");
      if (rm) {
        var row = rm.closest("[data-wish-row]");
        if (row && $$("[data-wish-row]", setupForm).length > 1) row.remove();
        else if (row) $$("input", row).forEach(function (i) { i.value = ""; });
        updateSetup();
      }
    });
    $$("[data-busy-on-click]", setupForm).forEach(function (btn) {
      btn.addEventListener("click", function () { setTimeout(function () { btn.classList.add("is-busy"); }, 0); });
    });
    syncPurpose();
    updateSetup();
  }

  // --------------------------------------------------------------- settings
  $$("[data-ai-check]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var out = $("[data-ai-result]");
      btn.classList.add("is-busy");
      api("GET", "/api/ai/check").then(function (d) {
        if (!out) return;
        out.className = "result " + (d.ok && d.model_available !== false ? "ok" : "err");
        out.textContent = d.ok && d.model_available !== false
          ? "✓ Работает: " + (d.resolved_model || d.model) + " на " + d.base_url
          : "✗ " + (d.error || (d.ok ? "Сервер работает, но модели «" + d.model + "» на нём нет" : "Нет ответа"));
        out.hidden = false;
      }).catch(function (e) { toast(e.message, "err"); })
        .then(function () { btn.classList.remove("is-busy"); });
    });
  });
  $$("[data-ai-detect]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var box = $("[data-ai-servers]");
      btn.classList.add("is-busy");
      api("GET", "/api/ai/detect").then(function (d) {
        if (!box) return;
        box.innerHTML = "";
        if (!d.servers.length) {
          box.textContent = "Не нашёл: LM Studio (порт 1234) и Ollama (порт 11434) не отвечают. Запусти сервер и нажми ещё раз.";
          return;
        }
        d.servers.forEach(function (s) {
          var head = document.createElement("div");
          head.className = "small";
          head.textContent = s.name + " · " + s.base_url + " — нажми на модель, чтобы подставить:";
          box.appendChild(head);
          var list = document.createElement("div");
          list.className = "model-list";
          s.models.forEach(function (m) {
            var b = document.createElement("button");
            b.type = "button";
            b.textContent = m;
            if (s.vision_models.indexOf(m) >= 0) { b.className = "vision"; b.title = "Видит фото"; }
            b.addEventListener("click", function () {
              var f = btn.closest("form");
              $('[name="provider"]', f).value = s.provider;
              $('[name="base_url"]', f).value = s.base_url;
              $('[name="model"]', f).value = m;
            });
            list.appendChild(b);
          });
          box.appendChild(list);
        });
      }).catch(function (e) { toast(e.message, "err"); })
        .then(function () { btn.classList.remove("is-busy"); });
    });
  });
})();
