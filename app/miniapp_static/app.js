// FindBenz Mini App — простой vanilla JS, без сборки.
// Авторизация — Telegram initData в заголовке X-Telegram-Init-Data на
// каждый запрос к /api/*. Вне Telegram initData пустой, все запросы к
// API получат 401 — это ожидаемо, приложение работает только изнутри бота.

const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}
const initData = (tg && tg.initData) || "";

const CATEGORY_LABELS = { all: "всё", petrol: "бензин", diesel: "дизель", gas: "газ" };

async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign(
    { "Content-Type": "application/json", "X-Telegram-Init-Data": initData },
    opts.headers || {}
  );
  const res = await fetch("/api" + path, Object.assign({}, opts, { headers }));
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    /* тело могло быть пустым */
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || res.statusText);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

const screenEl = document.getElementById("screen");
const badgeEl = document.getElementById("tariff-badge");

document.querySelectorAll(".tab-btn").forEach(function (btn) {
  btn.addEventListener("click", function () {
    switchScreen(btn.dataset.screen);
  });
});

const screens = { scan: renderScan, zones: renderZones, alerts: renderAlerts, tariff: renderTariff };

function switchScreen(name) {
  document.querySelectorAll(".tab-btn").forEach(function (b) {
    b.classList.toggle("active", b.dataset.screen === name);
  });
  screens[name]();
}

async function refreshBadge() {
  try {
    const t = await api("/tariff");
    if (t.paid_active) badgeEl.textContent = "✅ до " + fmtDate(t.paid_until);
    else if (t.on_trial) badgeEl.textContent = "🎁 триал до " + fmtDate(t.trial_ends_at);
    else badgeEl.textContent = "⛔️ нет доступа";
  } catch (e) {
    badgeEl.textContent = "";
  }
}

// ---------------------------------------------------------------------
// Экран 1: «Сканировать области»
// ---------------------------------------------------------------------

async function renderScan() {
  screenEl.innerHTML = '<div class="loading">Загрузка…</div>';
  try {
    const data = await api("/scan-targets");
    const zonesHtml =
      data.zones
        .map(function (z) {
          return (
            '<button class="list-item" data-scan-zone="' + z.id + '">🗂 ' +
            escapeHtml(z.name) + ' <span class="hint">(' + z.station_count + " АЗС)</span></button>"
          );
        })
        .join("") || '<div class="empty">Зон пока нет — создайте на вкладке «Добавить зону»</div>';

    const areasHtml =
      data.areas
        .map(function (a) {
          const name = typeof a === "string" ? a : a.area;
          return '<button class="list-item" data-scan-area="' + escapeHtml(name) + '">📍 ' + escapeHtml(name) + "</button>";
        })
        .join("") || '<div class="empty">Пока нет известных областей</div>';

    screenEl.innerHTML =
      '<section><h2>Мои зоны</h2><div class="list">' + zonesHtml + "</div></section>" +
      '<section><h2>Все области</h2><div class="list">' + areasHtml + "</div></section>" +
      '<div id="scan-result"></div>';

    screenEl.querySelectorAll("[data-scan-area]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        startScan({ area: btn.dataset.scanArea });
      });
    });
    screenEl.querySelectorAll("[data-scan-zone]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        startScan({ zone_id: Number(btn.dataset.scanZone) });
      });
    });
  } catch (e) {
    screenEl.innerHTML = '<div class="error">Не удалось загрузить: ' + escapeHtml(e.message) + "</div>";
  }
}

async function startScan(body) {
  const resultEl = document.getElementById("scan-result");
  resultEl.innerHTML = '<div class="loading">Запускаю скан…</div>';
  try {
    const res = await api("/scan", { method: "POST", body: JSON.stringify(body) });
    pollScanSession(res.session_id, resultEl);
  } catch (e) {
    const msg = e.data && e.data.error === "no_access" ? "Нужна активная подписка — откройте вкладку «Тариф»" : e.message;
    resultEl.innerHTML = '<div class="error">' + escapeHtml(msg) + "</div>";
  }
}

function pollScanSession(sessionId, resultEl) {
  resultEl.innerHTML = '<div class="loading">Сканирую… (обычно 5–20 сек)</div>';
  const startedAt = Date.now();

  async function tick() {
    try {
      const data = await api("/scan-sessions/" + sessionId);
      renderScanResult(data, resultEl);
      if (!data.finished) {
        if (Date.now() - startedAt < 90000) {
          setTimeout(tick, 3000);
        } else {
          resultEl.insertAdjacentHTML("beforeend", '<div class="hint">Данные долго не приходят, откройте вкладку заново позже.</div>');
        }
      }
    } catch (e) {
      resultEl.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
    }
  }
  tick();
}

function renderScanResult(data, resultEl) {
  resultEl.innerHTML = data.areas
    .map(function (a) {
      if (a.status !== "done") {
        return '<div class="area-block"><h3>' + escapeHtml(a.area) + '</h3><div class="hint">⏳ идёт сканирование…</div></div>';
      }
      const stations = a.stations || [];
      const list = stations.length
        ? stations
            .map(function (s) {
              const addr = s.address ? ' — <span class="hint">' + escapeHtml(s.address) + "</span>" : "";
              return '<div class="station">⛽️ ' + escapeHtml(s.name || s.api_id) + addr + "</div>";
            })
            .join("")
        : '<div class="hint">Топлива сейчас нет</div>';
      return '<div class="area-block"><h3>' + escapeHtml(a.area) + "</h3>" + list + "</div>";
    })
    .join("");
}

// ---------------------------------------------------------------------
// Экран 2: «Добавить зону»
// ---------------------------------------------------------------------

let zonesCache = [];
let editingZoneId = null;
let editingZoneName = "";
let editingZoneStationIds = new Set();
let editSelectedBrands = [];
let editSearchDebounce = null;

async function renderZones() {
  screenEl.innerHTML = '<div class="loading">Загрузка…</div>';
  try {
    const zonesRes = await api("/zones");
    zonesCache = zonesRes.zones;

    screenEl.innerHTML =
      "<section><h2>Мои зоны</h2>" +
      '<div class="list" id="zones-list">' + renderZonesListHtml() + "</div>" +
      '<div class="row"><input id="new-zone-name" placeholder="Название новой зоны" maxlength="100">' +
      '<button id="create-zone-btn" class="primary-btn" style="margin-top:0;width:auto">Создать</button></div>' +
      '<div class="hint" style="margin-top:6px">Станции добавляются внутри самой зоны — откройте её кнопкой «Редактировать».</div>' +
      "</section>";

    bindZonesListEvents();
    document.getElementById("create-zone-btn").addEventListener("click", createZoneFlow);
  } catch (e) {
    screenEl.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

function renderZonesListHtml() {
  return (
    zonesCache
      .map(function (z) {
        return (
          '<div class="list-item-row"><span>🗂 ' + escapeHtml(z.name) +
          ' <span class="hint">(' + z.station_count + ')</span></span>' +
          '<button class="icon-btn" data-open-zone="' + z.id + '">Редактировать</button>' +
          '<button class="icon-btn danger" data-del-zone="' + z.id + '">✕</button></div>'
        );
      })
      .join("") || '<div class="empty">Зон ещё нет — создайте первую ниже</div>'
  );
}

function bindZonesListEvents() {
  screenEl.querySelectorAll("[data-del-zone]").forEach(function (b) {
    b.addEventListener("click", function () {
      deleteZoneFlow(Number(b.dataset.delZone));
    });
  });
  screenEl.querySelectorAll("[data-open-zone]").forEach(function (b) {
    b.addEventListener("click", function () {
      openZoneFlow(Number(b.dataset.openZone));
    });
  });
}

async function createZoneFlow() {
  const input = document.getElementById("new-zone-name");
  const name = input.value.trim();
  if (!name) return;
  try {
    const res = await api("/zones", { method: "POST", body: JSON.stringify({ name: name }) });
    await renderZones();
    // Сразу открываем редактор новой зоны — не нужно создавать и потом
    // ещё раз искать её в списке, чтобы добавить станции.
    openZoneFlow(res.id);
  } catch (e) {
    alert(e.message);
  }
}

async function deleteZoneFlow(zoneId) {
  if (!confirm("Удалить зону?")) return;
  try {
    await api("/zones/" + zoneId, { method: "DELETE" });
    renderZones();
  } catch (e) {
    alert(e.message);
  }
}

// ---- Редактор зоны: текущие станции + поиск/фильтр/выбор-всё тут же ----

async function openZoneFlow(zoneId) {
  const zone = zonesCache.find(function (z) {
    return z.id === zoneId;
  });
  editingZoneId = zoneId;
  editingZoneName = zone ? zone.name : "";
  editSelectedBrands = [];

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = '<div class="modal-content"><div class="loading">Загрузка…</div></div>';
  document.body.appendChild(modal);

  await refreshZoneEditor(modal);
}

async function refreshZoneEditor(modal) {
  const content = modal.querySelector(".modal-content");
  try {
    const res = await api("/zones/" + editingZoneId + "/stations");
    editingZoneStationIds = new Set(res.stations.map(function (s) { return s.station_id; }));

    const currentListHtml =
      res.stations
        .map(function (s) {
          return (
            '<div class="list-item-row"><span>⛽️ ' + escapeHtml(s.name) +
            ' <span class="hint">' + escapeHtml(s.address || "") + "</span></span>" +
            '<button class="icon-btn danger" data-remove-station="' + escapeHtml(s.station_id) + '">✕</button></div>'
          );
        })
        .join("") || '<div class="empty">Пока пусто — добавьте станции ниже</div>';

    content.innerHTML =
      '<h3>🗂 ' + escapeHtml(editingZoneName) + "</h3>" +
      '<div class="list" id="zone-current-stations">' + currentListHtml + "</div>" +
      '<h3 style="margin-top:16px">Добавить станции</h3>' +
      '<div class="row">' +
      '<input id="zone-search" placeholder="Поиск по адресу или названию">' +
      '<button id="zone-brand-filter-btn" class="secondary-btn" style="margin-top:0;width:auto">Фирма</button>' +
      "</div>" +
      '<div id="zone-search-results" class="list" style="margin-top:10px">' +
      '<div class="empty">Начните вводить адрес/название или выберите «Фирма»</div></div>' +
      '<button id="zone-select-all-btn" class="secondary-btn" style="display:none">Выбрать всё</button>' +
      '<button id="zone-add-selected-btn" class="primary-btn" style="display:none">Добавить выбранные (<span id="zone-selected-count">0</span>)</button>' +
      '<button class="secondary-btn" id="close-modal">Закрыть</button>';

    content.querySelectorAll("[data-remove-station]").forEach(function (b) {
      b.addEventListener("click", async function () {
        await api("/zones/" + editingZoneId + "/stations/" + encodeURIComponent(b.dataset.removeStation), { method: "DELETE" });
        refreshZoneEditor(modal);
      });
    });
    content.querySelector("#close-modal").addEventListener("click", function () {
      modal.remove();
      renderZones(); // обновить счётчики станций в списке зон
    });
    content.querySelector("#zone-brand-filter-btn").addEventListener("click", function () {
      openBrandFilterFor(modal);
    });
    content.querySelector("#zone-search").addEventListener("input", function (e) {
      clearTimeout(editSearchDebounce);
      const value = e.target.value;
      editSearchDebounce = setTimeout(function () {
        runZoneStationSearch(value, modal);
      }, 350);
    });
    content.querySelector("#zone-select-all-btn").addEventListener("click", function () {
      content.querySelectorAll("[data-station-checkbox]").forEach(function (cb) {
        cb.checked = true;
      });
      updateSelectedCount(modal);
    });
    content.querySelector("#zone-add-selected-btn").addEventListener("click", async function () {
      const ids = Array.prototype.map.call(
        content.querySelectorAll("[data-station-checkbox]:checked"),
        function (cb) { return cb.dataset.stationCheckbox; }
      );
      if (!ids.length) return;
      try {
        await api("/zones/" + editingZoneId + "/stations/bulk", {
          method: "POST",
          body: JSON.stringify({ station_ids: ids }),
        });
        refreshZoneEditor(modal);
      } catch (e) {
        alert(e.message);
      }
    });
  } catch (e) {
    content.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

function updateSelectedCount(modal) {
  const count = modal.querySelectorAll("[data-station-checkbox]:checked").length;
  const countEl = modal.querySelector("#zone-selected-count");
  if (countEl) countEl.textContent = String(count);
  const addBtn = modal.querySelector("#zone-add-selected-btn");
  if (addBtn) addBtn.style.display = count > 0 ? "block" : "none";
}

async function openBrandFilterFor(zoneModal) {
  const panel = document.createElement("div");
  panel.className = "modal";
  panel.innerHTML = '<div class="modal-content"><div class="loading">Загрузка…</div></div>';
  document.body.appendChild(panel);

  try {
    const res = await api("/catalog/brands");
    const content = panel.querySelector(".modal-content");
    const rows = res.brands
      .map(function (b, i) {
        const checked = editSelectedBrands.indexOf(b.brand) !== -1 ? "checked" : "";
        return (
          '<label class="list-item-row"><span>' + (i + 1) + ". " + escapeHtml(b.brand) +
          ' <span class="hint">(' + b.count + ")</span></span>" +
          '<input type="checkbox" data-brand="' + escapeHtml(b.brand) + '" ' + checked + "></label>"
        );
      })
      .join("") || '<div class="empty">Каталог станций пока пуст</div>';

    content.innerHTML =
      "<h3>Фирма</h3>" +
      '<div class="hint" style="margin-bottom:8px">Отсортировано по популярности</div>' +
      '<div class="list">' + rows + "</div>" +
      '<button class="secondary-btn" id="clear-brands">Сбросить</button>' +
      '<button class="primary-btn" id="apply-brands">Применить</button>';

    content.querySelector("#clear-brands").addEventListener("click", function () {
      editSelectedBrands = [];
      panel.remove();
      runZoneStationSearch(zoneModal.querySelector("#zone-search").value, zoneModal);
    });
    content.querySelector("#apply-brands").addEventListener("click", function () {
      editSelectedBrands = Array.prototype.map.call(
        content.querySelectorAll("[data-brand]:checked"),
        function (cb) { return cb.dataset.brand; }
      );
      panel.remove();
      runZoneStationSearch(zoneModal.querySelector("#zone-search").value, zoneModal);
    });
  } catch (e) {
    panel.querySelector(".modal-content").innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

async function runZoneStationSearch(query, modal) {
  const resultsEl = modal.querySelector("#zone-search-results");
  if (!resultsEl) return; // модалка уже могла закрыться, пока ждали дебаунс

  const q = (query || "").trim();
  const selectAllBtn = modal.querySelector("#zone-select-all-btn");
  const addBtn = modal.querySelector("#zone-add-selected-btn");

  if (!q && editSelectedBrands.length === 0) {
    resultsEl.innerHTML = '<div class="empty">Начните вводить адрес/название или выберите «Фирма»</div>';
    if (selectAllBtn) selectAllBtn.style.display = "none";
    if (addBtn) addBtn.style.display = "none";
    return;
  }

  resultsEl.innerHTML = '<div class="loading">Ищу…</div>';
  try {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    editSelectedBrands.forEach(function (b) {
      params.append("brand", b);
    });
    const res = await api("/catalog/search?" + params.toString());
    // Не показываем то, что уже добавлено в эту зону.
    const filtered = res.stations.filter(function (s) {
      return !editingZoneStationIds.has(s.station_id);
    });

    resultsEl.innerHTML =
      filtered
        .map(function (s) {
          return (
            '<label class="list-item-row"><span>⛽️ ' + escapeHtml(s.brand ? s.brand + " — " : "") + escapeHtml(s.name) +
            ' <span class="hint">' + escapeHtml(s.address || "") + "</span></span>" +
            '<input type="checkbox" data-station-checkbox="' + escapeHtml(s.station_id) + '"></label>'
          );
        })
        .join("") || '<div class="empty">Ничего не найдено (или всё уже в зоне)</div>';

    resultsEl.querySelectorAll("[data-station-checkbox]").forEach(function (cb) {
      cb.addEventListener("change", function () {
        updateSelectedCount(modal);
      });
    });

    if (selectAllBtn) selectAllBtn.style.display = filtered.length ? "block" : "none";
    updateSelectedCount(modal);
  } catch (e) {
    resultsEl.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

// ---------------------------------------------------------------------
// Экран 3: «Мои оповещения»
// ---------------------------------------------------------------------

async function renderAlerts() {
  screenEl.innerHTML = '<div class="loading">Загрузка…</div>';
  try {
    const [subsRes, zonesRes, areasRes] = await Promise.all([
      api("/subscriptions"),
      api("/zones"),
      api("/catalog/areas"),
    ]);

    const zonesHtml =
      zonesRes.zones
        .map(function (z) {
          return (
            '<div class="list-item-row"><span>🗂 ' + escapeHtml(z.name) +
            ' <span class="hint">(' + z.station_count + " АЗС)</span></span>" +
            '<span class="hint">авто ✅</span></div>'
          );
        })
        .join("") || '<div class="empty">Зон нет — создайте на вкладке «Добавить зону», и они появятся тут автоматически</div>';

    const subsHtml =
      subsRes.subscriptions
        .map(function (s) {
          const areaLabel = s.area === "all" ? "🌍 все области" : "📍 " + escapeHtml(s.area);
          return '<div class="list-item-row"><span>' + areaLabel + " — " + (CATEGORY_LABELS[s.category] || s.category) + "</span></div>";
        })
        .join("") || '<div class="empty">Подписок по областям пока нет</div>';

    const areaOptions = areasRes.areas
      .map(function (a) {
        return '<option value="' + escapeHtml(a) + '">' + escapeHtml(a) + "</option>";
      })
      .join("");

    screenEl.innerHTML =
      "<section><h2>Мои зоны</h2>" +
      '<div class="hint" style="margin-bottom:6px">Оповещения по своим заправкам приходят автоматически, отдельно подписываться не нужно</div>' +
      '<div class="list">' + zonesHtml + "</div>" +
      "</section>" +
      "<section><h2>Подписка по области</h2>" +
      '<div class="list">' + subsHtml + "</div>" +
      '<div class="row">' +
      '<select id="sub-area"><option value="all">🌍 все области</option>' + areaOptions + "</select>" +
      '<select id="sub-category">' +
      '<option value="all">всё</option><option value="petrol">бензин</option>' +
      '<option value="diesel">дизель</option><option value="gas">газ</option>' +
      "</select>" +
      "</div>" +
      '<button id="add-sub-btn" class="primary-btn">Подписаться</button>' +
      (subsRes.subscriptions.length ? '<button id="unsub-all-btn" class="danger-btn">Отказаться от всех подписок по области</button>' : "") +
      "</section>";

    document.getElementById("add-sub-btn").addEventListener("click", async function () {
      const area = document.getElementById("sub-area").value;
      const category = document.getElementById("sub-category").value;
      try {
        await api("/subscriptions", { method: "POST", body: JSON.stringify({ area: area, category: category }) });
        renderAlerts();
      } catch (e) {
        if (e.data && e.data.error === "no_access") {
          alert("Нужна активная подписка на тариф — откройте вкладку «Тариф»");
        } else {
          alert(e.message);
        }
      }
    });

    const unsubBtn = document.getElementById("unsub-all-btn");
    if (unsubBtn) {
      unsubBtn.addEventListener("click", async function () {
        if (!confirm("Отписаться от всех подписок по области? Зоны это не затронет.")) return;
        await api("/subscriptions", { method: "DELETE" });
        renderAlerts();
      });
    }
  } catch (e) {
    screenEl.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

// ---------------------------------------------------------------------
// Экран 4: «Тариф и коины»
// ---------------------------------------------------------------------

async function renderTariff() {
  screenEl.innerHTML = '<div class="loading">Загрузка…</div>';
  try {
    const t = await api("/tariff");
    let statusHtml;
    if (t.on_trial) statusHtml = "🎁 Пробный доступ активен (до " + fmtDate(t.trial_ends_at) + ")";
    else if (t.paid_active) statusHtml = "✅ Подписка активна до " + fmtDate(t.paid_until);
    else statusHtml = "⛔️ Доступ закончился";

    const packagesHtml = t.coin_packages
      .map(function (p) {
        return '<button class="list-item" data-pkg="' + p.id + '">' + p.coins + " коинов — " + p.price_rub + "₽</button>";
      })
      .join("");

    screenEl.innerHTML =
      "<section><h2>Статус</h2>" +
      "<p>" + statusHtml + "</p>" +
      "<p>💰 Баланс: <b>" + t.coins + "</b> коинов</p>" +
      '<button id="buy-btn" class="primary-btn">Купить подписку на ' + t.subscription_days + " дн (" + t.subscription_cost_coins + " коинов)</button>" +
      "</section>" +
      '<section><h2>Пополнить коины</h2><div class="list">' + packagesHtml + "</div></section>";

    document.getElementById("buy-btn").addEventListener("click", async function () {
      try {
        await api("/tariff/purchase", { method: "POST" });
        renderTariff();
        refreshBadge();
      } catch (e) {
        if (e.data && e.data.error === "not_enough_coins") {
          alert("Не хватает коинов: нужно " + e.data.cost + ", у вас " + e.data.coins);
        } else {
          alert(e.message);
        }
      }
    });

    screenEl.querySelectorAll("[data-pkg]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        try {
          await api("/tariff/topup", { method: "POST", body: JSON.stringify({ package_id: btn.dataset.pkg }) });
        } catch (e) {
          alert((e.data && e.data.message) || "Оплата пока не подключена");
        }
      });
    });
  } catch (e) {
    screenEl.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

// ---------------------------------------------------------------------
// init
// ---------------------------------------------------------------------

if (!initData) {
  const tgPresent = !!window.Telegram;
  const waPresent = !!tg;
  const diag =
    "Telegram JS SDK: " + (tgPresent ? "загрузился" : "НЕ загрузился") +
    " · WebApp объект: " + (waPresent ? "есть" : "нет") +
    (waPresent ? " · версия клиента: " + (tg.version || "?") + " · платформа: " + (tg.platform || "?") : "");
  document.body.insertAdjacentHTML(
    "afterbegin",
    '<div class="warn-banner">⚠️ Открой это приложение через кнопку в Telegram-боте — вне Telegram оно не авторизуется.' +
      '<br><span style="opacity:.7;font-size:11px">' + escapeHtml(diag) + "</span></div>"
  );
}

switchScreen("scan");
refreshBadge();
