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
          resultEl.insertAdjacentHTML("beforeend", '<div class="hint">Скрапер долго отвечает, откройте вкладку заново позже.</div>');
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
        return '<div class="area-block"><h3>' + escapeHtml(a.area) + '</h3><div class="hint">⏳ ждём скрапер…</div></div>';
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

async function renderZones() {
  screenEl.innerHTML = '<div class="loading">Загрузка…</div>';
  try {
    const [zonesRes, areasRes] = await Promise.all([api("/zones"), api("/catalog/areas")]);
    zonesCache = zonesRes.zones;

    const zonesHtml =
      zonesCache
        .map(function (z) {
          return (
            '<div class="list-item-row"><span>🗂 ' + escapeHtml(z.name) +
            ' <span class="hint">(' + z.station_count + ')</span></span>' +
            '<button class="icon-btn" data-open-zone="' + z.id + '">Открыть</button>' +
            '<button class="icon-btn danger" data-del-zone="' + z.id + '">✕</button></div>'
          );
        })
        .join("") || '<div class="empty">Зон ещё нет</div>';

    const areaOptions = areasRes.areas
      .map(function (a) {
        return '<option value="' + escapeHtml(a) + '">' + escapeHtml(a) + "</option>";
      })
      .join("");

    screenEl.innerHTML =
      "<section><h2>Мои зоны</h2>" +
      '<div class="list" id="zones-list">' + zonesHtml + "</div>" +
      '<div class="row"><input id="new-zone-name" placeholder="Название новой зоны" maxlength="100">' +
      '<button id="create-zone-btn" class="primary-btn" style="margin-top:0;width:auto">Создать</button></div>' +
      "</section>" +
      "<section><h2>Добавить заправку в зону</h2>" +
      '<select id="area-select"><option value="">— выберите область —</option>' + areaOptions + "</select>" +
      '<div id="stations-list" class="list" style="margin-top:10px"></div>' +
      "</section>";

    document.getElementById("create-zone-btn").addEventListener("click", createZoneFlow);
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
    document.getElementById("area-select").addEventListener("change", onAreaSelect);
  } catch (e) {
    screenEl.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

async function createZoneFlow() {
  const input = document.getElementById("new-zone-name");
  const name = input.value.trim();
  if (!name) return;
  try {
    await api("/zones", { method: "POST", body: JSON.stringify({ name: name }) });
    renderZones();
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

async function openZoneFlow(zoneId) {
  try {
    const res = await api("/zones/" + zoneId + "/stations");
    const list =
      res.stations
        .map(function (s) {
          return (
            '<div class="list-item-row"><span>⛽️ ' + escapeHtml(s.name) +
            ' <span class="hint">' + escapeHtml(s.address || "") + "</span></span>" +
            '<button class="icon-btn danger" data-remove-station="' + escapeHtml(s.station_id) + '">✕</button></div>'
          );
        })
        .join("") || '<div class="empty">Пока пусто — добавьте станции ниже</div>';

    const modal = document.createElement("div");
    modal.className = "modal";
    modal.innerHTML =
      '<div class="modal-content"><h3>Станции в зоне</h3><div class="list">' + list + "</div>" +
      '<button class="secondary-btn" id="close-modal">Закрыть</button></div>';
    document.body.appendChild(modal);

    modal.querySelector("#close-modal").addEventListener("click", function () {
      modal.remove();
    });
    modal.querySelectorAll("[data-remove-station]").forEach(function (b) {
      b.addEventListener("click", async function () {
        await api("/zones/" + zoneId + "/stations/" + encodeURIComponent(b.dataset.removeStation), { method: "DELETE" });
        modal.remove();
        openZoneFlow(zoneId);
      });
    });
  } catch (e) {
    alert(e.message);
  }
}

async function onAreaSelect(e) {
  const area = e.target.value;
  const listEl = document.getElementById("stations-list");
  if (!area) {
    listEl.innerHTML = "";
    return;
  }
  listEl.innerHTML = '<div class="loading">Загрузка…</div>';
  try {
    const res = await api("/catalog?area=" + encodeURIComponent(area));
    const zoneOptions = zonesCache
      .map(function (z) {
        return '<option value="' + z.id + '">' + escapeHtml(z.name) + "</option>";
      })
      .join("");

    listEl.innerHTML =
      res.stations
        .map(function (s) {
          return (
            '<div class="list-item-row"><span>⛽️ ' + escapeHtml(s.name) +
            ' <span class="hint">' + escapeHtml(s.address || "") + "</span></span>" +
            '<select data-add-station="' + escapeHtml(s.station_id) + '"><option value="">+ в зону…</option>' +
            zoneOptions + "</select></div>"
          );
        })
        .join("") || '<div class="empty">Станции в этой области ещё не известны</div>';

    listEl.querySelectorAll("[data-add-station]").forEach(function (sel) {
      sel.addEventListener("change", async function (ev) {
        const zoneId = ev.target.value;
        if (!zoneId) return;
        try {
          await api("/zones/" + zoneId + "/stations", {
            method: "POST",
            body: JSON.stringify({ station_id: sel.dataset.addStation }),
          });
          const row = sel.closest(".list-item-row");
          row.classList.add("flash-ok");
          setTimeout(function () {
            row.classList.remove("flash-ok");
          }, 700);
          ev.target.value = "";
        } catch (err) {
          alert(err.message);
        }
      });
    });
  } catch (e) {
    listEl.innerHTML = '<div class="error">' + escapeHtml(e.message) + "</div>";
  }
}

// ---------------------------------------------------------------------
// Экран 3: «Мои оповещения»
// ---------------------------------------------------------------------

async function renderAlerts() {
  screenEl.innerHTML = '<div class="loading">Загрузка…</div>';
  try {
    const res = await api("/subscriptions");
    const list =
      res.subscriptions
        .map(function (s) {
          const areaLabel = s.area === "all" ? "🌍 все области" : "📍 " + escapeHtml(s.area);
          return '<div class="list-item-row"><span>' + areaLabel + " — " + (CATEGORY_LABELS[s.category] || s.category) + "</span></div>";
        })
        .join("") || '<div class="empty">Подписок пока нет — оформите на вкладке «Сканировать»</div>';

    screenEl.innerHTML =
      "<section><h2>Мои оповещения</h2>" +
      '<div class="list">' + list + "</div>" +
      (res.subscriptions.length ? '<button id="unsub-all-btn" class="danger-btn">Отказаться от всех</button>' : "") +
      "</section>";

    const btn = document.getElementById("unsub-all-btn");
    if (btn) {
      btn.addEventListener("click", async function () {
        if (!confirm("Отписаться от всех уведомлений?")) return;
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
  document.body.insertAdjacentHTML(
    "afterbegin",
    '<div class="warn-banner">⚠️ Открой это приложение через кнопку в Telegram-боте — вне Telegram оно не авторизуется.</div>'
  );
}

switchScreen("scan");
refreshBadge();
