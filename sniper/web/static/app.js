/* ZX Spectrum Watcher — Frontend */

(function () {
  "use strict";

  // -----------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------
  const listings = {};        // key -> listing object
  const prevTotals = {};      // key -> last known total (for price-drop detection)
  const seenKeys = new Set(); // keys we've already encountered this session
  let sortColumn = null;
  let sortDescending = false;
  let initialLoadComplete = false;

  // Mirror of server-side settings; updated locally for instant UI response.
  const settings = {
    theme: "light",
    search_term: "zx spectrum",
    filters: { text: "", price_min: null, price_max: null, deals_only: false, spectrums_only: true },
    sort: { column: null, descending: false },
    notify: { enabled: false, sound: true },
  };

  // DOM refs
  const root = document.documentElement;
  const searchInput = document.getElementById("search-input");
  const filterInput = document.getElementById("filter-input");
  const priceMinInput = document.getElementById("price-min");
  const priceMaxInput = document.getElementById("price-max");
  const chkSpectrums = document.getElementById("chk-spectrums");
  const chkDeals = document.getElementById("chk-deals");
  const sourceCheckboxContainer = document.getElementById("source-checkboxes");
  const sourceCheckboxes = {};  // source name -> checkbox element (built dynamically)
  const btnGo = document.getElementById("btn-go");
  const btnStop = document.getElementById("btn-stop");
  const btnExport = document.getElementById("btn-export");
  const btnClear = document.getElementById("btn-clear");
  const btnTheme = document.getElementById("btn-theme");
  const btnNotify = document.getElementById("btn-notify");
  const btnView = document.getElementById("btn-view");
  const countLabel = document.getElementById("listing-count");
  const tbody = document.getElementById("listings-body");
  const tableContainer = document.getElementById("table-container");
  const tileContainer = document.getElementById("tile-container");
  const tileGrid = document.getElementById("tile-grid");
  const consoleEl = document.getElementById("console");
  const statusText = document.getElementById("status-text");
  const progressBar = document.getElementById("progress-bar");
  const toastContainer = document.getElementById("toast-container");

  // -----------------------------------------------------------------------
  // Settings persistence (debounced, deep-merged)
  // -----------------------------------------------------------------------

  let saveTimer = null;
  let pending = {};

  function mergeInto(target, src) {
    Object.keys(src).forEach(function (k) {
      if (src[k] && typeof src[k] === "object" && !Array.isArray(src[k])) {
        target[k] = mergeInto(target[k] || {}, src[k]);
      } else {
        target[k] = src[k];
      }
    });
    return target;
  }

  function saveSettings(partial) {
    mergeInto(settings, partial);
    mergeInto(pending, partial);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSettings, 400);
  }

  function flushSettings() {
    const body = pending;
    pending = {};
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(function () {});
  }

  // -----------------------------------------------------------------------
  // Theme
  // -----------------------------------------------------------------------

  function setTheme(theme, save) {
    settings.theme = theme === "dark" ? "dark" : "light";
    root.dataset.theme = settings.theme;
    btnTheme.textContent = settings.theme === "dark" ? "☀️" : "🌙";
    btnTheme.title = settings.theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    if (save) saveSettings({ theme: settings.theme });
  }

  // -----------------------------------------------------------------------
  // Console logging
  // -----------------------------------------------------------------------

  function logMessage(msg, isError) {
    const now = new Date();
    const ts = now.toISOString().slice(0, 19).replace("T", " ");
    const line = document.createElement("div");
    line.className = "console-line" + (isError ? " console-error" : "");
    line.innerHTML = '<span class="console-ts">[' + ts + "]</span> " + escapeHtml(msg);
    consoleEl.appendChild(line);
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : s;
    return d.innerHTML;
  }

  // -----------------------------------------------------------------------
  // Notifications (toast + optional sound + optional browser notification)
  // -----------------------------------------------------------------------

  let audioCtx = null;

  function playBeep() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.connect(g);
      g.connect(audioCtx.destination);
      o.type = "sine";
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.2, audioCtx.currentTime + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.25);
      o.start();
      o.stop(audioCtx.currentTime + 0.26);
    } catch (e) {
      /* audio not available */
    }
  }

  function showToast(title, body) {
    const el = document.createElement("div");
    el.className = "toast";
    el.innerHTML =
      '<div class="toast-title">' + escapeHtml(title) + "</div>" +
      '<div class="toast-body">' + escapeHtml(body) + "</div>";
    toastContainer.appendChild(el);
    setTimeout(function () {
      el.classList.add("toast-out");
      setTimeout(function () { el.remove(); }, 300);
    }, 5000);
  }

  function notifyEvent(reason, item) {
    const price = item.total_eur != null ? "€" + item.total_eur.toFixed(2) : "";
    const title = reason + (price ? " · " + price : "");
    showToast(title, item.title || item.key);
    if (settings.notify.sound) playBeep();
    if ("Notification" in window && Notification.permission === "granted") {
      try {
        new Notification(title, { body: item.title || item.key });
      } catch (e) {
        /* ignore */
      }
    }
  }

  function setNotify(enabled, save) {
    settings.notify.enabled = enabled;
    settings.notify.sound = enabled; // sound rides with the toggle
    btnNotify.classList.toggle("on", enabled);
    btnNotify.textContent = enabled ? "🔔 Notify" : "🔕 Notify";
    btnNotify.setAttribute("aria-pressed", enabled ? "true" : "false");
    if (enabled && "Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(function () {});
    }
    if (save) saveSettings({ notify: { enabled: enabled, sound: enabled } });
  }

  // -----------------------------------------------------------------------
  // Status & progress
  // -----------------------------------------------------------------------

  function setStatus(text, current, total) {
    statusText.textContent = text;
    document.title = "ZX Spectrum Watcher — " + text;
    if (total && current != null) {
      progressBar.removeAttribute("class");
      progressBar.max = total;
      progressBar.value = current;
    } else {
      progressBar.classList.add("indeterminate");
      progressBar.removeAttribute("value");
    }
  }

  function setFetchState(running) {
    btnGo.disabled = running;
    btnStop.disabled = !running;
    if (!running) {
      setStatus("Idle");
      progressBar.value = 0;
      progressBar.max = 100;
      progressBar.classList.remove("indeterminate");
    }
  }

  // -----------------------------------------------------------------------
  // Shipping estimates per source → destination
  // -----------------------------------------------------------------------

  // Extra shipping cost (EUR) to add ON TOP of the listing's own ship_eur
  // to estimate delivery to each destination.  0 = domestic / already included.
  var SHIP_EXTRA = {
    //                        Venlo(NL)  Cornwall(UK)
    "Marktplaats":            [0,        18],
    "2dehands":               [5,        18],
    "Tweakers":               [0,        18],
    "Vinted":                 [0,        12],
    "eBay":                   [8,        12],
    "Kleinanzeigen":          [8,        15],
    "Catawiki":               [10,       15],
    "Leboncoin":              [12,       15],
    "Shpock":                 [8,        10],
    "Subito":                 [12,       18],
    "Gumtree":                [18,       0],
    "Wallapop":               [15,       20],
    "Allegro":                [12,       18],
  };

  function estimateTotal(item, destIdx) {
    // destIdx: 0 = Venlo, 1 = Cornwall
    var price = item.price_eur != null ? item.price_eur : 0;
    var ship = item.ship_eur != null ? item.ship_eur : 0;
    var extra = (SHIP_EXTRA[item.source] || [0, 0])[destIdx] || 0;
    // If the listing already has shipping, assume it covers domestic;
    // for the other destination we replace with the estimate.
    if (destIdx === 0) {
      // Venlo: for NL/BE sources, use listed ship; for foreign, use estimate
      return round2(price + ship + extra);
    } else {
      // UK: for UK sources, use listed ship; for foreign, use estimate
      if (item.source === "Gumtree") return round2(price + ship);
      return round2(price + extra);
    }
  }

  function round2(v) { return Math.round(v * 100) / 100; }

  // -----------------------------------------------------------------------
  // Table rendering
  // -----------------------------------------------------------------------

  function fmtPrice(v) {
    return v != null ? v.toFixed(2) : "";
  }

  function createRow(item, opts) {
    opts = opts || {};
    const tr = document.createElement("tr");
    tr.id = "row-" + CSS.escape(item.key);
    tr.dataset.key = item.key;
    if (item.is_deal) tr.classList.add("deal");
    if (opts.isNew) tr.classList.add("row-new");

    const totalClass = "cell-total" + (opts.dropped ? " price-drop" : "");
    const dropArrow = opts.dropped
      ? '<span class="drop-arrow" title="was €' +
        (opts.dropFrom != null ? opts.dropFrom.toFixed(2) : "?") +
        '">▼</span>'
      : "";

    var thumbHtml = item.thumb_url
      ? '<img src="' + escapeHtml(item.thumb_url) + '" loading="lazy" alt="">'
      : '<img class="lazy-thumb" data-key="' + escapeHtml(item.key) + '" src="" alt="" style="display:none">';

    tr.innerHTML =
      '<td class="cell-thumb">' + thumbHtml + "</td>" +
      '<td class="cell-title" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + "</td>" +
      '<td class="cell-price">' + fmtPrice(item.price_eur) + "</td>" +
      '<td class="cell-ship">' + fmtPrice(item.ship_eur) + "</td>" +
      '<td class="' + totalClass + '">' + dropArrow + fmtPrice(estimateTotal(item, 0)) +
      (item.price_eur != null ? ' <span class="uk-total">(' + fmtPrice(estimateTotal(item, 1)) + ')</span>' : '') +
      "</td>" +
      '<td class="cell-type">' + escapeHtml(item.type) + "</td>" +
      '<td class="cell-trend">' + escapeHtml(item.trend) + "</td>" +
      '<td class="cell-source">' + escapeHtml(item.source) + "</td>" +
      '<td class="cell-link"><a href="' + escapeHtml(item.link) + '" target="_blank" rel="noopener">Open</a></td>';

    return tr;
  }

  // -----------------------------------------------------------------------
  // Tile rendering
  // -----------------------------------------------------------------------

  var viewMode = "table"; // "table" or "tiles"

  function createTile(item) {
    var div = document.createElement("div");
    div.className = "tile" + (item.is_deal ? " deal" : "");
    div.id = "tile-" + CSS.escape(item.key);
    div.dataset.key = item.key;

    var imgHtml = item.thumb_url
      ? '<img class="tile-img" src="' + escapeHtml(item.thumb_url) + '" loading="lazy" alt="">'
      : '<div class="tile-img-placeholder" data-key="' + escapeHtml(item.key) + '">📦</div>';

    var venloTotal = item.price_eur != null ? estimateTotal(item, 0) : null;
    var ukTotal = item.price_eur != null ? estimateTotal(item, 1) : null;
    var priceText = venloTotal != null ? "€" + venloTotal.toFixed(2) : "";
    var ukText = ukTotal != null ? "(€" + ukTotal.toFixed(2) + ")" : "";

    div.innerHTML = imgHtml +
      '<div class="tile-body">' +
        '<div class="tile-title" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + '</div>' +
        '<div class="tile-meta">' +
          '<span class="tile-price">' + priceText + ' <span class="tile-uk-price">' + ukText + '</span></span>' +
          '<span class="tile-source">' + escapeHtml(item.source) + '</span>' +
        '</div>' +
        '<div class="tile-type">' + escapeHtml(item.type) + '</div>' +
      '</div>';

    div.addEventListener("click", function () {
      if (item.link) window.open(item.link, "_blank");
    });

    return div;
  }

  function setView(mode, save) {
    viewMode = mode;
    if (mode === "tiles") {
      tableContainer.classList.add("hidden");
      tileContainer.classList.remove("hidden");
      btnView.textContent = "▤ Tiles";
      rebuildTiles();
    } else {
      tileContainer.classList.add("hidden");
      tableContainer.classList.remove("hidden");
      btnView.textContent = "▦ Table";
    }
    if (save) saveSettings({ view: mode });
  }

  function rebuildTiles() {
    tileGrid.innerHTML = "";
    // Sort tiles the same way as the table
    var items = Object.keys(listings).map(function (k) { return listings[k]; }).filter(shouldShow);
    if (sortColumn) {
      var numCols = { price_eur: 1, ship_eur: 1, total_eur: 1 };
      items.sort(function (a, b) {
        var va = a[sortColumn], vb = b[sortColumn];
        if (numCols[sortColumn]) {
          va = va != null ? va : Infinity;
          vb = vb != null ? vb : Infinity;
          return sortDescending ? vb - va : va - vb;
        }
        va = (va || "").toLowerCase();
        vb = (vb || "").toLowerCase();
        if (va < vb) return sortDescending ? 1 : -1;
        if (va > vb) return sortDescending ? -1 : 1;
        return 0;
      });
    }
    items.forEach(function (item) { tileGrid.appendChild(createTile(item)); });
  }

  function upsertTile(item) {
    var existing = document.getElementById("tile-" + CSS.escape(item.key));
    var newTile = createTile(item);
    if (!shouldShow(item)) {
      if (existing) existing.remove();
      return;
    }
    if (existing) {
      existing.replaceWith(newTile);
    } else {
      tileGrid.appendChild(newTile);
    }
  }

  function removeTile(key) {
    var el = document.getElementById("tile-" + CSS.escape(key));
    if (el) el.remove();
  }

  // -----------------------------------------------------------------------
  // Upsert (both views)
  // -----------------------------------------------------------------------

  function upsertRow(item) {
    const isNew = !seenKeys.has(item.key);
    const prev = prevTotals[item.key];
    const dropped = prev != null && item.total_eur != null && item.total_eur < prev - 0.001;

    seenKeys.add(item.key);
    if (item.total_eur != null) prevTotals[item.key] = item.total_eur;
    listings[item.key] = item;

    const newRow = createRow(item, {
      isNew: isNew && initialLoadComplete,
      dropped: dropped,
      dropFrom: prev,
    });

    const existing = document.getElementById("row-" + CSS.escape(item.key));
    if (existing) {
      existing.replaceWith(newRow);
    } else {
      tbody.appendChild(newRow);
    }

    applyFilterToRow(item.key, newRow);
    updateCount();

    // Keep tile view in sync
    if (viewMode === "tiles") upsertTile(item);

    // Queue thumbnail discovery for items without images
    if (!item.thumb_url && initialLoadComplete) {
      setTimeout(function () { observeMissingThumbs(); }, 100);
    }

    // Fire notifications only after the initial DB load is in place.
    if (initialLoadComplete && settings.notify.enabled) {
      if (isNew && item.is_deal) notifyEvent("New deal", item);
      else if (dropped) notifyEvent(item.is_deal ? "Deal price drop" : "Price drop", item);
    }
  }

  function removeRow(key) {
    delete listings[key];
    const row = document.getElementById("row-" + CSS.escape(key));
    if (row) row.remove();
    removeTile(key);
    updateCount();
  }

  function clearTable() {
    tbody.innerHTML = "";
    tileGrid.innerHTML = "";
    Object.keys(listings).forEach(function (k) { delete listings[k]; });
    updateCount();
  }

  // -----------------------------------------------------------------------
  // Filtering
  // -----------------------------------------------------------------------

  /** Build source filter checkboxes from the server's source list. */
  function buildSourceCheckboxes(names) {
    sourceCheckboxContainer.innerHTML = "";
    names.forEach(function (name) {
      if (sourceCheckboxes[name]) return; // already built
      var label = document.createElement("label");
      label.className = "checkbox-label";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true; // default on
      cb.id = "chk-src-" + name.replace(/\s+/g, "-").toLowerCase();
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" " + name));
      sourceCheckboxContainer.appendChild(label);
      sourceCheckboxes[name] = cb;
      cb.addEventListener("change", onFilterChange);
    });
  }

  function sourceKey(name) { return "show_" + name.replace(/\s+/g, "_").toLowerCase(); }

  function readFilters() {
    var minRaw = priceMinInput.value.trim();
    var maxRaw = priceMaxInput.value.trim();
    var f = {
      text: filterInput.value.trim(),
      deals_only: chkDeals.checked,
      spectrums_only: chkSpectrums.checked,
      price_min: minRaw === "" ? null : parseFloat(minRaw),
      price_max: maxRaw === "" ? null : parseFloat(maxRaw),
    };
    Object.keys(sourceCheckboxes).forEach(function (name) {
      f[sourceKey(name)] = sourceCheckboxes[name].checked;
    });
    return f;
  }

  function shouldShow(item) {
    var f = readFilters();
    var query = f.text.toLowerCase();

    if (query && !(item.title || "").toLowerCase().includes(query)) return false;

    if (f.spectrums_only && item.is_relevant === false) return false;

    // Check source toggle — if we have a checkbox for this source, respect it.
    // Sources without a checkbox (unexpected) are always shown.
    var sk = sourceKey(item.source);
    if (sk in f && !f[sk]) return false;

    if (f.deals_only && !item.is_deal) return false;

    var val = item.total_eur != null ? item.total_eur : item.price_eur;
    if (f.price_min != null && !isNaN(f.price_min) && (val == null || val < f.price_min)) return false;
    if (f.price_max != null && !isNaN(f.price_max) && (val == null || val > f.price_max)) return false;

    return true;
  }

  function applyFilterToRow(key, row) {
    const item = listings[key];
    if (!item) return;
    row.style.display = shouldShow(item) ? "" : "none";
  }

  function applyFilter() {
    tbody.querySelectorAll("tr").forEach(function (row) {
      const key = row.dataset.key;
      if (key && listings[key]) {
        row.style.display = shouldShow(listings[key]) ? "" : "none";
      }
    });
    // Rebuild tiles when filter changes (simpler than show/hide each)
    if (viewMode === "tiles") rebuildTiles();
    updateCount();
  }

  function onFilterChange() {
    applyFilter();
    saveSettings({ filters: readFilters() });
  }

  function updateCount() {
    const total = Object.keys(listings).length;
    let visible = 0;
    tbody.querySelectorAll("tr").forEach(function (row) {
      if (row.style.display !== "none") visible++;
    });
    countLabel.textContent = visible === total
      ? total + " listings"
      : visible + " of " + total + " listings";
  }

  // -----------------------------------------------------------------------
  // Sorting
  // -----------------------------------------------------------------------

  const numericCols = new Set(["price_eur", "ship_eur", "total_eur"]);

  function applySort(col, descending, save) {
    sortColumn = col;
    sortDescending = descending;

    document.querySelectorAll("#listings-table th").forEach(function (th) {
      th.classList.remove("sort-asc", "sort-desc");
    });
    const th = document.querySelector('th[data-col="' + col + '"]');
    if (th) th.classList.add(descending ? "sort-desc" : "sort-asc");

    const rows = Array.from(tbody.querySelectorAll("tr"));
    rows.sort(function (a, b) {
      const ia = listings[a.dataset.key];
      const ib = listings[b.dataset.key];
      if (!ia || !ib) return 0;
      let va = ia[col];
      let vb = ib[col];
      if (numericCols.has(col)) {
        va = va != null ? va : Infinity;
        vb = vb != null ? vb : Infinity;
        return descending ? vb - va : va - vb;
      }
      va = (va || "").toLowerCase();
      vb = (vb || "").toLowerCase();
      if (va < vb) return descending ? 1 : -1;
      if (va > vb) return descending ? -1 : 1;
      return 0;
    });
    rows.forEach(function (row) { tbody.appendChild(row); });

    // Keep tile view in sync with sort order
    if (viewMode === "tiles") rebuildTiles();

    if (save) saveSettings({ sort: { column: col, descending: descending } });
  }

  function sortTable(col) {
    const descending = sortColumn === col ? !sortDescending : false;
    applySort(col, descending, true);
  }

  // -----------------------------------------------------------------------
  // SSE connection
  // -----------------------------------------------------------------------

  function connectSSE() {
    const es = new EventSource("/api/events");

    es.addEventListener("upsert", function (e) {
      upsertRow(JSON.parse(e.data));
    });
    es.addEventListener("delete", function (e) {
      const data = JSON.parse(e.data);
      removeRow(data.key);
      logMessage("Removed: " + data.key);
    });
    es.addEventListener("status", function (e) {
      const data = JSON.parse(e.data);
      setStatus(data.text, data.current, data.total);
      logMessage(data.text);
    });
    es.addEventListener("done", function (e) {
      const data = JSON.parse(e.data);
      setFetchState(false);
      logMessage(data.text || "Fetch complete");
    });
    es.addEventListener("error", function (e) {
      if (e.data) {
        const data = JSON.parse(e.data);
        logMessage("ERROR: " + data.text, true);
      }
    });
    es.onerror = function () {
      logMessage("SSE connection lost. Reconnecting...", true);
    };
  }

  // -----------------------------------------------------------------------
  // API calls
  // -----------------------------------------------------------------------

  function doSearch() {
    const term = searchInput.value.trim();
    if (!term) return;
    // Only clear when the search term actually changed
    if (term.toLowerCase() !== (settings.search_term || "").toLowerCase()) {
      clearTable();
    }
    setFetchState(true);
    setStatus("Fetching…");
    logMessage("Search: " + term);
    saveSettings({ search_term: term });
    fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ term: term }),
    }).catch(function (err) {
      logMessage("Search request failed: " + err, true);
      setFetchState(false);
    });
  }

  function doStop() {
    fetch("/api/stop", { method: "POST" }).catch(function () {});
    logMessage("Stop requested.");
  }

  function doExport() {
    window.open("/api/listings/csv", "_blank");
  }

  // -----------------------------------------------------------------------
  // Apply persisted settings to the UI
  // -----------------------------------------------------------------------

  function applySettings(s) {
    if (!s || typeof s !== "object") return;
    mergeInto(settings, s);

    setTheme(settings.theme, false);
    if (settings.search_term) searchInput.value = settings.search_term;

    const f = settings.filters || {};
    filterInput.value = f.text || "";
    chkDeals.checked = !!f.deals_only;
    chkSpectrums.checked = f.spectrums_only !== false;
    priceMinInput.value = f.price_min != null ? f.price_min : "";
    priceMaxInput.value = f.price_max != null ? f.price_max : "";
    // Restore per-source toggles
    Object.keys(sourceCheckboxes).forEach(function (name) {
      var sk = sourceKey(name);
      sourceCheckboxes[name].checked = f[sk] !== false;
    });

    setNotify(!!(settings.notify && settings.notify.enabled), false);

    // Restore view mode
    if (settings.view === "tiles") setView("tiles", false);
  }

  // -----------------------------------------------------------------------
  // Initial load
  // -----------------------------------------------------------------------

  function loadListings() {
    return fetch("/api/listings")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!settings.search_term && data.search_term) {
          searchInput.value = data.search_term;
        }
        // Build source checkboxes from the server's registered source list
        if (data.sources && data.sources.length) {
          buildSourceCheckboxes(data.sources);
          // Re-apply saved filter state to the newly built checkboxes
          var f = settings.filters || {};
          Object.keys(sourceCheckboxes).forEach(function (name) {
            var sk = sourceKey(name);
            if (sk in f) sourceCheckboxes[name].checked = f[sk] !== false;
          });
        }
        if (data.fetch_running) setFetchState(true);
        (data.listings || []).forEach(function (item) { upsertRow(item); });
        logMessage("Loaded " + (data.listings || []).length + " listings from database.");
      })
      .catch(function (err) {
        logMessage("Failed to load listings: " + err, true);
      });
  }

  // -----------------------------------------------------------------------
  // Event bindings
  // -----------------------------------------------------------------------

  function bindEvents() {
    btnGo.addEventListener("click", doSearch);
    btnStop.addEventListener("click", doStop);
    btnExport.addEventListener("click", doExport);
    btnClear.addEventListener("click", clearTable);
    btnView.addEventListener("click", function () {
      setView(viewMode === "table" ? "tiles" : "table", true);
    });
    btnTheme.addEventListener("click", function () {
      setTheme(settings.theme === "dark" ? "light" : "dark", true);
    });
    btnNotify.addEventListener("click", function () {
      setNotify(!settings.notify.enabled, true);
    });

    searchInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") doSearch();
    });

    filterInput.addEventListener("input", onFilterChange);
    priceMinInput.addEventListener("input", onFilterChange);
    priceMaxInput.addEventListener("input", onFilterChange);
    chkSpectrums.addEventListener("change", onFilterChange);
    chkDeals.addEventListener("change", onFilterChange);
    // Source checkbox listeners are bound dynamically in buildSourceCheckboxes()

    document.addEventListener("keydown", function (e) {
      const typing = e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName);
      if (e.key === "F5") {
        e.preventDefault();
        doSearch();
      } else if (e.key === "Escape" && !typing) {
        doStop();
      }
    });

    document.querySelectorAll("#listings-table th.sortable").forEach(function (th) {
      th.addEventListener("click", function () { sortTable(th.dataset.col); });
    });
  }

  // -----------------------------------------------------------------------
  // Lazy thumbnail fetcher — discovers images for items that have none
  // -----------------------------------------------------------------------

  var thumbQueue = [];         // keys waiting to be fetched
  var thumbFetching = false;   // only one fetch at a time
  var thumbAttempted = new Set(); // don't retry failed keys

  function queueThumbFetch(key) {
    if (thumbAttempted.has(key)) return;
    thumbAttempted.add(key);
    thumbQueue.push(key);
    drainThumbQueue();
  }

  function drainThumbQueue() {
    if (thumbFetching || !thumbQueue.length) return;
    thumbFetching = true;
    var key = thumbQueue.shift();

    fetch("/api/fetch-thumb/" + encodeURIComponent(key))
      .then(function (r) {
        if (!r.ok) return;
        // Success — update the item's thumb_url and refresh its row/tile
        var item = listings[key];
        if (item) {
          item.thumb_url = "/api/thumbnail/" + encodeURIComponent(key);
          // Update table row image
          var img = document.querySelector('#row-' + CSS.escape(key) + ' .cell-thumb img');
          if (img) {
            img.src = item.thumb_url;
            img.style.display = "";
            img.classList.remove("lazy-thumb");
          }
          // Update tile image
          var placeholder = document.querySelector('#tile-' + CSS.escape(key) + ' .tile-img-placeholder');
          if (placeholder) {
            var newImg = document.createElement("img");
            newImg.className = "tile-img";
            newImg.src = item.thumb_url;
            newImg.loading = "lazy";
            placeholder.replaceWith(newImg);
          }
        }
      })
      .catch(function () { /* ignore failures */ })
      .then(function () {
        thumbFetching = false;
        // Small delay between fetches to be polite
        setTimeout(drainThumbQueue, 800);
      });
  }

  // Use IntersectionObserver to trigger fetches for visible items without thumbs
  var thumbObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        var key = entry.target.dataset.key;
        if (key) {
          queueThumbFetch(key);
          thumbObserver.unobserve(entry.target);
        }
      }
    });
  }, { rootMargin: "200px" });

  function observeMissingThumbs() {
    // Table rows
    document.querySelectorAll(".lazy-thumb").forEach(function (img) {
      var key = img.dataset.key;
      if (key && !thumbAttempted.has(key)) thumbObserver.observe(img);
    });
    // Tile placeholders
    document.querySelectorAll(".tile-img-placeholder[data-key]").forEach(function (el) {
      var key = el.dataset.key;
      if (key && !thumbAttempted.has(key)) thumbObserver.observe(el);
    });
  }

  // -----------------------------------------------------------------------
  // Boot
  // -----------------------------------------------------------------------

  function init() {
    bindEvents();
    fetch("/api/settings")
      .then(function (r) { return r.json(); })
      .then(applySettings)
      .catch(function (err) { logMessage("Failed to load settings: " + err, true); })
      .then(function () { return loadListings(); })
      .then(function () {
        initialLoadComplete = true;
        if (settings.sort && settings.sort.column) {
          applySort(settings.sort.column, !!settings.sort.descending, false);
        }
        applyFilter();
        connectSSE();
        // Start discovering thumbnails for items that don't have one
        setTimeout(observeMissingThumbs, 500);
      });
  }

  init();
})();
