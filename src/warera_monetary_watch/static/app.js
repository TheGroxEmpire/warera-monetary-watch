const config = globalThis.MONETARY_WATCH ?? {};
const basePath = config.basePath ?? "/monetary-watch";
const selectedCountryCode = config.selectedCountryCode ?? null;

let currentCountryCode = selectedCountryCode;
let hasFetched = false;
let hasPendingFetchScope = false;
let refreshSequence = 0;
let countryDataset = null;

const els = {
  coverageLabel: document.getElementById("coverage-label"),
  dataCoverageLabel: document.getElementById("data-coverage-label"),
  collectorStatusLabel: document.getElementById("collector-status-label"),
  fetchNotice: document.getElementById("fetch-notice"),
  fetchButton: document.getElementById("fetch-tax-button"),
  overviewPanel: document.getElementById("overview-panel"),
  overviewBody: document.querySelector("#overview-table tbody"),
  summaryGrid: document.getElementById("summary-grid"),
  itemsBody: document.querySelector("#items-table tbody"),
  ownersBody: document.querySelector("#owners-table tbody"),
  chart: document.getElementById("timeseries-chart"),
  countryTitle: document.getElementById("country-title"),
  countrySubtitle: document.getElementById("country-subtitle"),
  countryDetail: document.getElementById("country-detail"),
  form: document.getElementById("filters-form"),
  countrySelect: document.getElementById("country-select"),
  itemSelect: document.getElementById("item-select"),
  ownerCountrySelect: document.getElementById("owner-country-select"),
  coreSelect: document.getElementById("core-select"),
  fromInput: document.getElementById("from-input"),
  toInput: document.getElementById("to-input"),
};

let dataWindow = {
  minFrom: null,
  maxTo: null,
};

function shouldShowLeaderboard() {
  return Boolean(els.overviewPanel) && !currentCountryCode;
}

function shouldShowLeaderboardFor(countryCode) {
  return Boolean(els.overviewPanel) && !countryCode;
}

function selectedCountryCodeFromForm() {
  return els.countrySelect.value || null;
}

function isoHourUtc(date) {
  return date.toISOString().slice(0, 16);
}

function parseDate(value) {
  if (!value) {
    return null;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function floorHourUtc(date) {
  const next = new Date(date);
  next.setUTCMinutes(0, 0, 0);
  return next;
}

function ceilHourUtc(date) {
  const next = floorHourUtc(date);
  if (next.getTime() < date.getTime()) {
    next.setUTCHours(next.getUTCHours() + 1);
  }
  return next;
}

function startOfWeekMondayUtc(date) {
  const monday = floorHourUtc(date);
  const daysSinceMonday = (monday.getUTCDay() + 6) % 7;
  monday.setUTCDate(monday.getUTCDate() - daysSinceMonday);
  monday.setUTCHours(0, 0, 0, 0);
  return monday;
}

function clampDate(date, minDate, maxDate) {
  if (minDate && date < minDate) {
    return new Date(minDate);
  }
  if (maxDate && date > maxDate) {
    return new Date(maxDate);
  }
  return date;
}

function defaultWindow() {
  const maxTo = dataWindow.maxTo ?? ceilHourUtc(new Date());
  const minFrom = dataWindow.minFrom;
  const from = clampDate(startOfWeekMondayUtc(maxTo), minFrom, maxTo);
  return {
    from: isoHourUtc(from),
    to: isoHourUtc(maxTo),
  };
}

function formatWindowLabel() {
  if (!els.fromInput.value || !els.toInput.value) {
    return "Choose filters, then fetch tax data.";
  }
  return `${els.fromInput.value.replace("T", " ")} to ${els.toInput.value.replace("T", " ")} UTC`;
}

function formatUtcLabel(date) {
  return isoHourUtc(date).replace("T", " ");
}

function applyDataBounds(filters, status) {
  const fromDate = parseDate(filters?.data_bounds?.from);
  const toDate = parseDate(filters?.data_bounds?.to);
  const minFrom = fromDate ? ceilHourUtc(fromDate) : null;
  const maxTo = toDate ? ceilHourUtc(toDate) : null;

  dataWindow = { minFrom, maxTo };

  if (minFrom) {
    els.fromInput.min = isoHourUtc(minFrom);
    els.toInput.min = isoHourUtc(minFrom);
  }
  if (maxTo) {
    els.fromInput.max = isoHourUtc(maxTo);
    els.toInput.max = isoHourUtc(maxTo);
  }

  if (minFrom && maxTo && minFrom < maxTo) {
    els.dataCoverageLabel.textContent = `${formatUtcLabel(minFrom)} to ${formatUtcLabel(maxTo)} UTC`;
  } else {
    els.dataCoverageLabel.textContent = "No complete hourly range yet.";
  }

  const cursorDate = parseDate(status?.cursor?.created_at);
  if (cursorDate) {
    els.collectorStatusLabel.textContent = `Latest collected event: ${cursorDate.toISOString().slice(0, 19).replace("T", " ")} UTC`;
  } else {
    els.collectorStatusLabel.textContent = "Collector has not recorded a cursor yet.";
  }
}

function clampInputsToDataBounds() {
  if (!dataWindow.minFrom || !dataWindow.maxTo) {
    return;
  }

  const currentFrom = parseDate(`${els.fromInput.value}:00Z`);
  const currentTo = parseDate(`${els.toInput.value}:00Z`);
  const from = currentFrom ? clampDate(currentFrom, dataWindow.minFrom, dataWindow.maxTo) : dataWindow.minFrom;
  let to = currentTo ? clampDate(currentTo, dataWindow.minFrom, dataWindow.maxTo) : dataWindow.maxTo;
  if (from >= to) {
    to = dataWindow.maxTo;
  }
  els.fromInput.value = isoHourUtc(from);
  els.toInput.value = isoHourUtc(to);
}

function formatMoney(value) {
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value || 0));
}

function formatInt(value) {
  return new Intl.NumberFormat("en-US").format(Number(value || 0));
}

function formatPercent(value) {
  return `${Number(value || 0).toFixed(1)}%`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function fetchJson(path, params = new URLSearchParams()) {
  const query = params.toString();
  const response = await fetch(`${basePath}${path}${query ? `?${query}` : ""}`);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed for ${path}`);
  }
  return response.json();
}

function selectedFetchParams() {
  const params = new URLSearchParams();
  if (els.fromInput.value) params.set("from", els.fromInput.value);
  if (els.toInput.value) params.set("to", els.toInput.value);
  return params;
}

function selectedAggregateParams({ includeItem = true, includeOwner = true } = {}) {
  const params = selectedFetchParams();
  if (includeItem && els.itemSelect.value) params.set("item", els.itemSelect.value);
  if (includeOwner && els.ownerCountrySelect.value) {
    params.set("owner_country", els.ownerCountrySelect.value);
  }
  if (els.coreSelect.value && els.coreSelect.value !== "all") params.set("core", els.coreSelect.value);
  return params;
}

function selectedDatasetFilters() {
  return {
    itemCode: els.itemSelect.value || null,
    ownerCountryId: els.ownerCountrySelect.value || null,
    coreFilter: els.coreSelect.value || "all",
  };
}

function datasetEntryMatches(entry, { itemCode, ownerCountryId, coreFilter }) {
  if (itemCode && entry.item_code !== itemCode) {
    return false;
  }
  if (ownerCountryId && entry.owner_country_id !== ownerCountryId) {
    return false;
  }
  if (coreFilter === "core" && !entry.is_core_region) {
    return false;
  }
  if (coreFilter === "noncore" && entry.is_core_region) {
    return false;
  }
  return true;
}

function filterDatasetEntries(entries, overrides = {}) {
  const filters = { ...selectedDatasetFilters(), ...overrides };
  return entries.filter((entry) => datasetEntryMatches(entry, filters));
}

function sumEntries(entries, key) {
  return entries.reduce((total, entry) => total + Number(entry[key] || 0), 0);
}

function entryTaxRate(entry) {
  const wagesPaid = Number(entry.wages_paid || 0);
  return wagesPaid ? (Number(entry.tax_income || 0) / wagesPaid) * 100 : 0;
}

function averageEntryTaxRate(entries, countryIncomeTaxRate) {
  if (entries.length === 0) {
    return 0;
  }
  const totalTax = sumEntries(entries, "tax_income");
  const totalWages = sumEntries(entries, "wages_paid");
  const effectiveRate = totalWages ? (totalTax / totalWages) : 0;
  // Convert effective rate to statutory rate: statutory = effective * (100 + statutory) / 100
  // This accounts for wages being gross amounts
  const statutoryRate = countryIncomeTaxRate ? effectiveRate * (100 + countryIncomeTaxRate) / 100 : effectiveRate;
  return statutoryRate * 100;
}

function groupEntries(entries, key) {
  return entries.reduce((groups, entry) => {
    const value = entry[key] ?? "unknown";
    if (!groups.has(value)) {
      groups.set(value, []);
    }
    groups.get(value).push(entry);
    return groups;
  }, new Map());
}

function buildSummaryFromDataset(payload) {
  const entries = filterDatasetEntries(payload.entries ?? []);
  const taxIncome = sumEntries(entries, "tax_income");
  const wagesPaid = sumEntries(entries, "wages_paid");
  const coreEntries = entries.filter((entry) => entry.is_core_region);
  const nonCoreEntries = entries.filter((entry) => !entry.is_core_region);

  // Convert effective rate to statutory rate
  const effectiveRate = wagesPaid ? (taxIncome / wagesPaid) : 0;
  const statutoryRate = payload.country_income_tax_rate ? effectiveRate * (100 + payload.country_income_tax_rate) / 100 : effectiveRate;

  return {
    country_id: payload.country_id,
    country_code: payload.country_code,
    country_name: payload.country_name,
    from: payload.from,
    to_exclusive: payload.to_exclusive,
    summary: {
      tax_income: taxIncome,
      wages_paid: wagesPaid,
      transactions: sumEntries(entries, "transactions"),
      companies: sumEntries(entries, "companies"),
      workers: sumEntries(entries, "workers"),
      items: new Set(entries.map((entry) => entry.item_code).filter(Boolean)).size,
      core_tax_income: sumEntries(coreEntries, "tax_income"),
      non_core_tax_income: sumEntries(nonCoreEntries, "tax_income"),
      avg_tax_rate: wagesPaid ? statutoryRate * 100 : 0,
    },
  };
}

function buildTimeseriesFromDataset(payload) {
  const entries = filterDatasetEntries(payload.entries ?? []);
  const points = Array.from(groupEntries(entries, "hour_start").entries())
    .map(([hourStart, hourEntries]) => ({
      hour_start: hourStart,
      tax_income: sumEntries(hourEntries, "tax_income"),
      wages_paid: sumEntries(hourEntries, "wages_paid"),
      transactions: sumEntries(hourEntries, "transactions"),
    }))
    .sort((a, b) => String(a.hour_start).localeCompare(String(b.hour_start)));

  return {
    country_code: payload.country_code,
    points,
  };
}

function buildItemsFromDataset(payload) {
  const entries = filterDatasetEntries(payload.entries ?? [], { itemCode: null });
  const totalTax = sumEntries(entries, "tax_income");
  const items = Array.from(groupEntries(entries, "item_code").entries())
    .map(([itemCode, itemEntries]) => {
      const taxIncome = sumEntries(itemEntries, "tax_income");
      return {
        item_code: itemCode,
        tax_income: taxIncome,
        wages_paid: sumEntries(itemEntries, "wages_paid"),
        avg_tax_rate: averageEntryTaxRate(itemEntries, payload.country_income_tax_rate),
        companies: sumEntries(itemEntries, "companies"),
        transactions: sumEntries(itemEntries, "transactions"),
        share: totalTax ? (taxIncome / totalTax) * 100 : 0,
      };
    })
    .sort((a, b) => b.tax_income - a.tax_income);

  return {
    country_code: payload.country_code,
    items,
  };
}

function buildOwnersFromDataset(payload) {
  const entries = filterDatasetEntries(payload.entries ?? [], { ownerCountryId: null });
  const totalTax = sumEntries(entries, "tax_income");
  const owners = Array.from(groupEntries(entries, "owner_country_id").entries())
    .map(([ownerCountryId, ownerEntries]) => {
      const firstEntry = ownerEntries[0] ?? {};
      const taxIncome = sumEntries(ownerEntries, "tax_income");
      return {
        owner_country_id: ownerCountryId,
        owner_country_code: firstEntry.owner_country_code ?? null,
        owner_country_name: firstEntry.owner_country_name ?? "Unknown",
        tax_income: taxIncome,
        wages_paid: sumEntries(ownerEntries, "wages_paid"),
        avg_tax_rate: averageEntryTaxRate(ownerEntries, payload.country_income_tax_rate),
        companies: sumEntries(ownerEntries, "companies"),
        transactions: sumEntries(ownerEntries, "transactions"),
        share: totalTax ? (taxIncome / totalTax) * 100 : 0,
      };
    })
    .sort((a, b) => b.tax_income - a.tax_income);

  return {
    country_code: payload.country_code,
    owners,
  };
}

function renderCountryDataset(payload) {
  els.countryDetail.hidden = false;
  renderSummary(buildSummaryFromDataset(payload));
  renderChart(buildTimeseriesFromDataset(payload));
  renderItems(buildItemsFromDataset(payload));
  renderOwners(buildOwnersFromDataset(payload));
}

function renderOverview(payload) {
  if (!els.overviewBody) {
    return;
  }

  const rows = payload.countries ?? [];
  if (rows.length === 0) {
    els.overviewBody.innerHTML = `<tr><td colspan="5" class="empty-state">No collected tax data yet for this range.</td></tr>`;
    return;
  }

  els.overviewBody.innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td><a class="country-link" href="${basePath}/countries/${encodeURIComponent(row.country_code)}">${escapeHtml(row.country_name)}</a></td>
          <td>${formatMoney(row.tax_income)}</td>
          <td>${formatMoney(row.wages_paid)}</td>
          <td>${formatPercent(row.avg_tax_rate)}</td>
          <td>${formatInt(row.transactions)}</td>
        </tr>
      `
    )
    .join("");
}

function renderSummary(payload) {
  const summary = payload.summary ?? {};
  els.countryTitle.textContent = `${payload.country_name} Tax Income`;
  els.countrySubtitle.textContent = "Wage-derived income tax grouped by item, owner origin, and core-region status.";

  const cards = [
    ["Total Tax Income", formatMoney(summary.tax_income), "Income tax captured from wages"],
    ["Total Wages Paid", formatMoney(summary.wages_paid), "Gross wages before tax"],
    ["Companies", formatInt(summary.companies), "Distinct companies in range"],
    ["Workers", formatInt(summary.workers), "Distinct wage earners in range"],
    ["Items", formatInt(summary.items), "Distinct produced items"],
    ["Core Region Tax", formatMoney(summary.core_tax_income), "Tax from core regions"],
    ["Non-Core Tax", formatMoney(summary.non_core_tax_income), "Tax from non-core regions"],
    ["Avg Tax Rate", formatPercent(summary.avg_tax_rate), "Effective income tax rate"],
  ];

  els.summaryGrid.innerHTML = cards
    .map(
      ([label, value, caption]) => `
        <article class="summary-card">
          <h3>${escapeHtml(label)}</h3>
          <strong>${escapeHtml(value)}</strong>
          <span>${escapeHtml(caption)}</span>
        </article>
      `
    )
    .join("");
}

function renderItems(payload) {
  const items = payload.items ?? [];
  if (items.length === 0) {
    els.itemsBody.innerHTML = `<tr><td colspan="7" class="empty-state">No item-level tax data for this range.</td></tr>`;
    return;
  }

  els.itemsBody.innerHTML = items
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.item_code)}</td>
          <td>${formatMoney(row.tax_income)}</td>
          <td>${formatMoney(row.wages_paid)}</td>
          <td>${formatPercent(row.avg_tax_rate)}</td>
          <td>${formatInt(row.companies)}</td>
          <td>${formatInt(row.transactions)}</td>
          <td>${formatPercent(row.share)}</td>
        </tr>
      `
    )
    .join("");
}

function renderOwners(payload) {
  const owners = payload.owners ?? [];
  if (owners.length === 0) {
    els.ownersBody.innerHTML = `<tr><td colspan="7" class="empty-state">No owner-country tax data for this range.</td></tr>`;
    return;
  }

  els.ownersBody.innerHTML = owners
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.owner_country_name)}</td>
          <td>${formatMoney(row.tax_income)}</td>
          <td>${formatMoney(row.wages_paid)}</td>
          <td>${formatPercent(row.avg_tax_rate)}</td>
          <td>${formatInt(row.companies)}</td>
          <td>${formatInt(row.transactions)}</td>
          <td>${formatPercent(row.share)}</td>
        </tr>
      `
    )
    .join("");
}

function renderChart(payload) {
  const points = payload.points ?? [];
  if (points.length === 0) {
    els.chart.innerHTML = `<div class="empty-state">No hourly trend data for this range.</div>`;
    return;
  }

  const width = 960;
  const height = 240;
  const padding = 16;
  const maxY = Math.max(
    ...points.flatMap((point) => [Number(point.tax_income || 0), Number(point.wages_paid || 0)]),
    1
  );

  const stepX = points.length === 1 ? 0 : (width - padding * 2) / (points.length - 1);
  const yFor = (value) => height - padding - (Number(value || 0) / maxY) * (height - padding * 2);

  const makePath = (key) =>
    points
      .map((point, index) => {
        const x = padding + stepX * index;
        const y = yFor(point[key]);
        return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
      })
      .join(" ");

  els.chart.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" aria-label="Hourly tax income chart">
      <path d="${makePath("tax_income")}" fill="none" stroke="#ffbf47" stroke-width="3" stroke-linecap="round"></path>
      <path d="${makePath("wages_paid")}" fill="none" stroke="#7de2d1" stroke-width="3" stroke-linecap="round"></path>
    </svg>
    <div class="chart-caption">
      <span><span class="legend-dot legend-tax"></span>Tax income</span>
      <span><span class="legend-dot legend-wage"></span>Wages paid</span>
    </div>
  `;
}

function populateCountries(countries) {
  const countryOptions = countries
    .map(
      (country) => `<option value="${escapeHtml(country.code)}">${escapeHtml(country.name)}</option>`
    )
    .join("");
  const ownerOptions = countries
    .map(
      (country) => `<option value="${escapeHtml(country.id)}">${escapeHtml(country.name)}</option>`
    )
    .join("");
  els.countrySelect.innerHTML = `<option value="">All countries</option>${countryOptions}`;
  els.ownerCountrySelect.innerHTML = `<option value="">All countries</option>${ownerOptions}`;
}

function populateItems(items) {
  const options = items
    .map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`)
    .join("");
  els.itemSelect.innerHTML = `<option value="">All items</option>${options}`;
}

function renderPreFetchState(message = "Waiting for filters to be applied.") {
  els.coverageLabel.textContent = formatWindowLabel();
  if (els.overviewPanel) {
    els.overviewPanel.hidden = !shouldShowLeaderboard();
  }
  if (els.overviewBody) {
    els.overviewBody.innerHTML = `<tr><td colspan="5" class="empty-state">Choose country and time, then click Fetch Tax Data.</td></tr>`;
  }
  els.summaryGrid.innerHTML = "";
  els.itemsBody.innerHTML = `<tr><td colspan="7" class="empty-state">Choose country and time, then click Fetch Tax Data.</td></tr>`;
  els.ownersBody.innerHTML = `<tr><td colspan="7" class="empty-state">Choose country and time, then click Fetch Tax Data.</td></tr>`;
  els.chart.innerHTML = `<div class="empty-state">Choose country and time, then click Fetch Tax Data.</div>`;
  els.countryDetail.hidden = !currentCountryCode;
  if (els.fetchNotice) {
    els.fetchNotice.textContent = message;
  }
}

function setFetchingState(isFetching) {
  if (els.fetchButton) {
    els.fetchButton.disabled = isFetching;
    els.fetchButton.textContent = isFetching ? "Fetching Tax Data..." : "Fetch Tax Data";
  }
  if (els.form) {
    els.form.classList.toggle("is-fetching", isFetching);
  }
  if (els.fetchNotice && isFetching) {
    els.fetchNotice.textContent = "Fetching tax data for the selected country and time range...";
  }
}

function syncPageUrl() {
  if (!globalThis.history?.replaceState) {
    return;
  }

  const nextPath = currentCountryCode
    ? `${basePath}/countries/${encodeURIComponent(currentCountryCode)}`
    : `${basePath}/`;

  if (globalThis.location.pathname !== nextPath) {
    globalThis.history.replaceState({}, "", nextPath);
  }
}

async function refreshAll(countryCode = currentCountryCode) {
  const refreshId = ++refreshSequence;
  const params = selectedFetchParams();
  const shouldRenderLeaderboard = shouldShowLeaderboardFor(countryCode);

  if (shouldRenderLeaderboard) {
    const overview = await fetchJson("/api/v1/overview", selectedAggregateParams());
    if (refreshId !== refreshSequence) {
      return false;
    }
    currentCountryCode = countryCode;
    syncPageUrl();
    els.coverageLabel.textContent = formatWindowLabel();
    if (els.overviewPanel) {
      els.overviewPanel.hidden = false;
    }
    countryDataset = null;
    renderOverview(overview);
    els.countryDetail.hidden = true;
    return true;
  } else if (countryCode) {
    const dataset = await fetchJson(`/api/v1/countries/${countryCode}/dataset`, params);
    if (refreshId !== refreshSequence) {
      return false;
    }
    currentCountryCode = countryCode;
    syncPageUrl();
    els.coverageLabel.textContent = formatWindowLabel();
    if (els.overviewPanel) {
      els.overviewPanel.hidden = true;
    }
    countryDataset = dataset;
    if (els.overviewBody) {
      els.overviewBody.innerHTML = "";
    }
    renderCountryDataset(dataset);
    return true;
  } else {
    currentCountryCode = countryCode;
    syncPageUrl();
    els.coverageLabel.textContent = formatWindowLabel();
    if (els.overviewPanel) {
      els.overviewPanel.hidden = true;
    }
    countryDataset = null;
    if (els.overviewBody) {
      els.overviewBody.innerHTML = "";
    }
    els.countryDetail.hidden = true;
    return true;
  }
}

function initializeFilters() {
  const { from, to } = defaultWindow();
  els.fromInput.value = from;
  els.toInput.value = to;
  if (currentCountryCode) {
    els.countrySelect.value = currentCountryCode;
  }
  renderPreFetchState();
}

async function bootstrap() {
  const [filters, status] = await Promise.all([
    fetchJson("/api/v1/filters"),
    fetchJson("/api/v1/status"),
  ]);
  applyDataBounds(filters, status);
  initializeFilters();
  populateCountries(filters.countries ?? []);
  populateItems(filters.items ?? []);
  if (currentCountryCode) {
    els.countrySelect.value = currentCountryCode;
  }
  renderPreFetchState();
}

async function fetchCurrentSelection() {
  const nextCountryCode = selectedCountryCodeFromForm();
  setFetchingState(true);
  try {
    const updated = await refreshAll(nextCountryCode);
    if (!updated) {
      return;
    }
    hasFetched = true;
    hasPendingFetchScope = false;
    if (els.fetchNotice) {
      els.fetchNotice.textContent = "Tax data updated.";
    }
  } finally {
    setFetchingState(false);
  }
}

async function handleDatasetFilterChange() {
  if (!hasFetched) {
    renderPreFetchState();
    return;
  }

  if (currentCountryCode && countryDataset) {
    renderCountryDataset(countryDataset);
    if (els.fetchNotice) {
      els.fetchNotice.textContent = hasPendingFetchScope
        ? "Filters applied to the currently fetched country data. Click Fetch Tax Data to load the pending country or time range."
        : "Filters applied.";
    }
    return;
  }

  if (hasPendingFetchScope) {
    if (els.fetchNotice) {
      els.fetchNotice.textContent = "Click Fetch Tax Data to load the pending country or time range.";
    }
    return;
  }

  setFetchingState(true);
  try {
    await refreshAll();
    if (els.fetchNotice) {
      els.fetchNotice.textContent = "Tax data updated.";
    }
  } finally {
    setFetchingState(false);
  }
}

function handleFetchScopeChange() {
  clampInputsToDataBounds();
  hasPendingFetchScope = true;
  if (!hasFetched) {
    renderPreFetchState("Country or time changed. Click Fetch Tax Data to load tax data.");
  } else if (els.fetchNotice) {
    els.fetchNotice.textContent = "Country or time changed. Click Fetch Tax Data to load tax data.";
  }
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await fetchCurrentSelection();
  } catch (error) {
    if (els.fetchNotice) {
      els.fetchNotice.textContent = `Could not fetch tax data: ${error.message}`;
    }
    throw error;
  }
});

[els.itemSelect, els.ownerCountrySelect, els.coreSelect].forEach((element) => {
  element.addEventListener("change", () => {
    handleDatasetFilterChange().catch((error) => {
      if (els.fetchNotice) {
        els.fetchNotice.textContent = `Could not apply filters: ${error.message}`;
      }
      throw error;
    });
  });
});

[els.countrySelect, els.fromInput, els.toInput].forEach((element) => {
  element.addEventListener("change", handleFetchScopeChange);
});

bootstrap().catch((error) => {
  console.error(error);
  if (els.fetchNotice) {
    els.fetchNotice.textContent = `Could not load Monetary Watch: ${error.message}`;
  }
});
