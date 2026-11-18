"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  connectionForm: $("#connectionForm"),
  deviceSelect: $("#deviceSelect"),
  manualDevice: $("#manualDevice"),
  baudrate: $("#baudrate"),
  connectButton: $("#connectButton"),
  demoButton: $("#demoButton"),
  disconnectButton: $("#disconnectButton"),
  refreshPorts: $("#refreshPorts"),
  formNote: $("#formNote"),
  connectionPill: $("#connectionPill"),
  connectionLabel: $("#connectionLabel"),
  packetCount: $("#packetCount"),
  byteCount: $("#byteCount"),
  dataRate: $("#dataRate"),
  lastSeen: $("#lastSeen"),
  terminal: $("#terminal"),
  terminalPlaceholder: $("#terminalPlaceholder"),
  pauseButton: $("#pauseButton"),
  clearButton: $("#clearButton"),
  exportButton: $("#exportButton"),
  streamState: $("#streamState"),
  toast: $("#toast"),
  canvas: $("#signalChart"),
  chartEmpty: $("#chartEmpty"),
  chartLegend: $("#chartLegend"),
  climateForm: $("#climateForm"),
  climateProfile: $("#climateProfile"),
  climateProfileName: $("#climateProfileName"),
  climateProfileSave: $("#climateProfileSave"),
  climateProfileDelete: $("#climateProfileDelete"),
  climateProfileHelp: $("#climateProfileHelp"),
  climateTarget: $("#climateTarget"),
  climateScan: $("#climateScan"),
  climateDevice: $("#climateDevice"),
  climateQuery: $("#climateQuery"),
  climateNote: $("#climateNote"),
  climateDot: $("#climateDot"),
  climateName: $("#climateName"),
  climateMeta: $("#climateMeta"),
  climatePower: $("#climatePower"),
  climateRoom: $("#climateRoom"),
  climateTargetTemp: $("#climateTargetTemp"),
  climateMode: $("#climateMode"),
  climateFan: $("#climateFan"),
  climateFlags: $("#climateFlags"),
  climateRaw: $("#climateRaw"),
  tapHomeDot: $("#tapHomeDot"),
  tapHomeStatus: $("#tapHomeStatus"),
  tapHomeEndpoint: $("#tapHomeEndpoint"),
};

const state = {
  status: null,
  entries: [],
  format: "text",
  paused: false,
  eventSource: null,
  rateWindow: [],
  sessionPackets: 0,
  sessionBytes: 0,
  toastTimer: null,
  climateDevices: [],
  climateProfiles: [],
};

const COLORS = ["#b8f231", "#63c6e7", "#ff9f58", "#e98bd6", "#aa9cff", "#ff746c"];

function showToast(message, isError = false) {
  clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.classList.add("show");
  state.toastTimer = setTimeout(() => elements.toast.classList.remove("show"), 3000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

async function loadPorts() {
  elements.refreshPorts.classList.add("loading");
  try {
    const { ports } = await api("/api/ports");
    const previous = elements.deviceSelect.value;
    elements.deviceSelect.replaceChildren();
    if (!ports.length) {
      elements.deviceSelect.add(new Option("Nem található soros port", ""));
      elements.formNote.textContent = "Csatlakoztasd az USB-eszközt, vagy add meg kézzel az útvonalát.";
    } else {
      elements.deviceSelect.add(new Option("Válassz portot", ""));
      ports.forEach((port) => {
        elements.deviceSelect.add(new Option(`${port.device} — ${port.description}`, port.device));
      });
      if (ports.some((port) => port.device === previous)) elements.deviceSelect.value = previous;
      elements.formNote.textContent = `${ports.length} elérhető soros port.`;
    }
    elements.formNote.classList.remove("error");
  } catch (error) {
    setFormError(error.message);
  } finally {
    elements.refreshPorts.classList.remove("loading");
  }
}

function setClimateNote(message, isError = false) {
  elements.climateNote.textContent = message;
  elements.climateNote.classList.toggle("error", isError);
  if (isError) showToast(message, true);
}

function renderClimateProfiles(profiles, selectedName = elements.climateProfile.value) {
  state.climateProfiles = profiles;
  elements.climateProfile.replaceChildren(new Option("Nincs mentett profil", ""));
  profiles.forEach((profile) => {
    elements.climateProfile.add(new Option(`${profile.name} · ${profile.ip}`, profile.name));
  });
  const selected = profiles.find((profile) => profile.name.toLowerCase() === selectedName.toLowerCase());
  if (selected) elements.climateProfile.value = selected.name;
  elements.climateProfileDelete.disabled = !selected;
}

function selectClimateProfile() {
  const profile = state.climateProfiles.find((item) => item.name === elements.climateProfile.value);
  elements.climateProfileDelete.disabled = !profile;
  if (!profile) return;
  elements.climateProfileName.value = profile.name;
  elements.climateTarget.value = profile.ip;
  elements.climateProfileHelp.textContent = `TapHome API: /api/taphome/ac/${profile.name}`;
}

async function loadClimateProfiles() {
  try {
    const result = await api("/api/ac/profiles");
    renderClimateProfiles(result.profiles || []);
    if (result.profiles?.length === 1) {
      elements.climateProfile.value = result.profiles[0].name;
      selectClimateProfile();
    }
  } catch (error) {
    setClimateNote(error.message, true);
  }
}

async function saveClimateProfile() {
  elements.climateProfileSave.disabled = true;
  try {
    const result = await api("/api/ac/profiles", {
      method: "POST",
      body: JSON.stringify({
        name: elements.climateProfileName.value.trim(),
        ip: elements.climateTarget.value.trim(),
      }),
    });
    renderClimateProfiles(result.profiles || [], result.profile.name);
    selectClimateProfile();
    showToast(`${result.profile.name} klímaprofil elmentve.`);
    loadTapHomeStatus();
  } catch (error) {
    setClimateNote(error.message, true);
  } finally {
    elements.climateProfileSave.disabled = false;
  }
}

async function deleteClimateProfile() {
  const name = elements.climateProfile.value;
  if (!name || !window.confirm(`Törlöd ezt a klímaprofilt: ${name}?`)) return;
  elements.climateProfileDelete.disabled = true;
  try {
    const result = await api("/api/ac/profiles/delete", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    renderClimateProfiles(result.profiles || [], "");
    elements.climateProfileName.value = "";
    elements.climateTarget.value = "";
    elements.climateProfileHelp.textContent = "A profilnév lesz az API és a TapHome sablon klímaazonosítója.";
    showToast(`${name} klímaprofil törölve.`);
    loadTapHomeStatus();
  } catch (error) {
    setClimateNote(error.message, true);
    elements.climateProfileDelete.disabled = false;
  }
}

function renderClimateDevices(devices) {
  const previous = elements.climateDevice.value;
  state.climateDevices = devices;
  elements.climateDevice.replaceChildren();
  if (!devices.length) {
    elements.climateDevice.add(new Option("Nem található Gree klíma", ""));
    elements.climateQuery.disabled = true;
    return;
  }
  devices.forEach((device) => {
    const detail = [device.name, device.model, device.ip].filter(Boolean).join(" · ");
    elements.climateDevice.add(new Option(detail, device.mac));
  });
  if (devices.some((device) => device.mac === previous)) elements.climateDevice.value = previous;
  elements.climateQuery.disabled = false;
}

async function loadClimateOverview() {
  try {
    const overview = await api("/api/ac/devices");
    if (overview.configuredTarget && !elements.climateTarget.value) elements.climateTarget.value = overview.configuredTarget;
    renderClimateDevices(overview.devices || []);
    if (!overview.available) setClimateNote("A Gree Wi-Fi kliens nincs telepítve a szerveren.", true);
  } catch (error) {
    setClimateNote(error.message, true);
  }
}

async function loadTapHomeStatus() {
  try {
    const gateway = await api("/api/taphome/status");
    elements.tapHomeDot.classList.toggle("online", gateway.running && gateway.connected);
    elements.tapHomeDot.classList.toggle("error", Boolean(gateway.error) || !gateway.available);
    elements.tapHomeEndpoint.textContent = `HTTP ${gateway.endpoint}`;
    if (!gateway.available) elements.tapHomeStatus.textContent = "A Gree Wi-Fi kliens nincs telepítve";
    else if (gateway.error) elements.tapHomeStatus.textContent = gateway.error;
    else if (gateway.connected) elements.tapHomeStatus.textContent = `Klímaadat elérhető · ${gateway.lastReadAt ? new Date(gateway.lastReadAt).toLocaleTimeString("hu-HU", { hour12: false }) : "—"}`;
    else if (gateway.running) elements.tapHomeStatus.textContent = `${gateway.profiles?.length || 0} klímaprofil · Packet Parser API kész`;
    else elements.tapHomeStatus.textContent = "HTTP gateway indításra vár";
  } catch (error) {
    elements.tapHomeDot.classList.add("error");
    elements.tapHomeStatus.textContent = error.message;
  }
}

function setClimateBusy(busy) {
  elements.climateScan.disabled = busy;
  elements.climateQuery.disabled = busy || !state.climateDevices.length;
}

async function scanClimate() {
  setClimateBusy(true);
  setClimateNote("Helyi Gree eszközök keresése…");
  try {
    const result = await api("/api/ac/scan", {
      method: "POST",
      body: JSON.stringify({ target: elements.climateTarget.value.trim() }),
    });
    renderClimateDevices(result.devices || []);
    if (!result.devices.length) {
      setClimateNote("Nem érkezett válasz. Ellenőrizd a Wi-Fi kapcsolatot és az IP-címet.", true);
    } else {
      setClimateNote(`${result.devices.length} Gree eszköz felderítve.`);
      showToast("A klíma elérhető a helyi hálózaton.");
    }
  } catch (error) {
    setClimateNote(error.message, true);
  } finally {
    setClimateBusy(false);
  }
}

const CLIMATE_LABELS = {
  auto: "Automata", cool: "Hűtés", dry: "Párátlanítás", fan: "Ventilátor", heat: "Fűtés",
  low: "Alacsony", mediumlow: "Közepes–alacsony", medium: "Közepes", mediumhigh: "Közepes–magas", high: "Magas",
};

function translatedClimateValue(value) {
  if (!value) return "—";
  return CLIMATE_LABELS[value] || value;
}

function temperature(value, unit) {
  return value === null || value === undefined ? "—" : `${value}${unit || "°C"}`;
}

function yesNo(value) {
  if (value === null || value === undefined) return "—";
  return value ? "BE" : "KI";
}

function renderClimateStatus(result) {
  const ac = result.state || {};
  const device = result.device || {};
  elements.climateDot.classList.add("online");
  elements.climateName.textContent = device.name || device.model || "Gree klíma";
  elements.climateMeta.textContent = [device.ip, device.model, result.firmware ? `FW ${result.firmware}` : null].filter(Boolean).join(" · ");
  elements.climatePower.textContent = ac.power ? "BEKAPCSOLVA" : "KIKAPCSOLVA";
  elements.climatePower.classList.toggle("on", Boolean(ac.power));
  elements.climateRoom.textContent = temperature(ac.currentTemperature, ac.temperatureUnit);
  elements.climateTargetTemp.textContent = temperature(ac.targetTemperature, ac.temperatureUnit);
  elements.climateMode.textContent = translatedClimateValue(ac.mode?.name);
  elements.climateFan.textContent = translatedClimateValue(ac.fanSpeed?.name);
  const flags = [["CSENDES", ac.quiet], ["TURBÓ", ac.turbo], ["ALVÁS", ac.sleep], ["FÉNY", ac.light]];
  elements.climateFlags.replaceChildren(...flags.map(([label, value]) => {
    const flag = document.createElement("span");
    flag.append(`${label} `);
    const mark = document.createElement("b");
    mark.textContent = yesNo(value);
    flag.append(mark);
    return flag;
  }));
  elements.climateRaw.textContent = JSON.stringify(result, null, 2);
}

async function queryClimate(event) {
  event.preventDefault();
  if (!elements.climateDevice.value) return;
  setClimateBusy(true);
  setClimateNote("Klímaállapot lekérése…");
  try {
    const identifier = encodeURIComponent(elements.climateDevice.value);
    const result = await api(`/api/ac/status?device=${identifier}`);
    renderClimateStatus(result);
    setClimateNote(`Frissítve: ${new Date(result.updatedAt).toLocaleTimeString("hu-HU", { hour12: false })}`);
  } catch (error) {
    elements.climateDot.classList.remove("online");
    setClimateNote(error.message, true);
  } finally {
    setClimateBusy(false);
  }
}

function setFormError(message) {
  elements.formNote.textContent = message;
  elements.formNote.classList.add("error");
  showToast(message, true);
}

function setBusy(busy) {
  elements.connectButton.disabled = busy;
  elements.demoButton.disabled = busy;
  elements.disconnectButton.disabled = busy;
}

function applyStatus(status) {
  state.status = status;
  state.sessionPackets = status.packets || 0;
  state.sessionBytes = status.bytes || 0;
  const connected = Boolean(status.connected);
  elements.connectionPill.classList.toggle("connected", connected);
  elements.connectionLabel.textContent = connected
    ? status.mode === "demo" ? "Próbaadat aktív" : `${status.device} · ${status.baudrate}`
    : "Nincs kapcsolat";
  elements.connectionForm.hidden = connected;
  elements.disconnectButton.hidden = !connected;
  elements.streamState.textContent = connected ? "ADATFOLYAM AKTÍV" : "ADATFOLYAM KÉSZ";
  $(".status-dot").classList.toggle("live", connected);

  elements.packetCount.textContent = formatInteger(state.sessionPackets);
  elements.byteCount.textContent = formatBytes(state.sessionBytes);
  if (status.lastPacketAt) updateLastSeen(status.lastPacketAt);
  else elements.lastSeen.textContent = "—";

  if (status.error) setFormError(status.error);
  else if (connected) {
    elements.formNote.classList.remove("error");
    elements.formNote.textContent = status.mode === "demo" ? "A beépített jelforrás fut." : "A soros port megnyitva.";
  }
}

async function connectSerial(event) {
  event.preventDefault();
  const device = elements.manualDevice.value.trim() || elements.deviceSelect.value;
  if (!device) {
    setFormError("Válassz portot, vagy adj meg egy eszközútvonalat.");
    return;
  }
  setBusy(true);
  try {
    const status = await api("/api/connect", {
      method: "POST",
      body: JSON.stringify({ device, baudrate: Number(elements.baudrate.value) }),
    });
    applyStatus(status);
    showToast(`Kapcsolódva: ${device}`);
  } catch (error) {
    setFormError(error.message);
  } finally {
    setBusy(false);
  }
}

async function startDemo() {
  setBusy(true);
  try {
    const status = await api("/api/demo", { method: "POST", body: "{}" });
    applyStatus(status);
    showToast("Próbaadat elindítva.");
  } catch (error) {
    setFormError(error.message);
  } finally {
    setBusy(false);
  }
}

async function disconnect() {
  setBusy(true);
  try {
    const status = await api("/api/disconnect", { method: "POST", body: "{}" });
    applyStatus(status);
    showToast("Kapcsolat bontva.");
  } catch (error) {
    setFormError(error.message);
  } finally {
    setBusy(false);
  }
}

function connectEventStream() {
  state.eventSource?.close();
  const source = new EventSource("/api/stream");
  state.eventSource = source;
  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.type === "state") applyStatus(event.status);
      if (event.type === "reading") handleReading(event);
    } catch (error) {
      console.warn("Hibás esemény", error);
    }
  };
  source.onerror = () => {
    elements.streamState.textContent = "ÚJRACSATLAKOZÁS…";
  };
  source.onopen = () => {
    elements.streamState.textContent = state.status?.connected ? "ADATFOLYAM AKTÍV" : "ADATFOLYAM KÉSZ";
  };
}

function handleReading(reading) {
  state.entries.push(reading);
  if (state.entries.length > 500) state.entries.shift();
  state.rateWindow.push({ time: performance.now(), bytes: reading.byteLength });
  trimRateWindow();

  if (!state.paused) appendTerminalRow(reading);
  chart.add(reading);

  state.sessionPackets += 1;
  state.sessionBytes += reading.byteLength;
  elements.packetCount.textContent = formatInteger(state.sessionPackets);
  elements.byteCount.textContent = formatBytes(state.sessionBytes);
  updateLastSeen(reading.receivedAt);
}

function appendTerminalRow(reading) {
  elements.terminalPlaceholder.hidden = true;
  const row = document.createElement("div");
  row.className = "terminal-row";
  row.dataset.sequence = reading.sequence;
  const date = new Date(reading.receivedAt);
  const content = state.format === "hex" ? reading.hex : visibleText(reading.text);
  row.innerHTML = `<time>${date.toLocaleTimeString("hu-HU", { hour12: false })}</time><span class="bytes">${reading.byteLength} B</span>`;
  const code = document.createElement("code");
  code.textContent = content;
  row.append(code);
  elements.terminal.append(row);
  while (elements.terminal.querySelectorAll(".terminal-row").length > 500) {
    elements.terminal.querySelector(".terminal-row")?.remove();
  }
  elements.terminal.scrollTop = elements.terminal.scrollHeight;
}

function visibleText(text) {
  return text.replace(/\r/g, "\\r").replace(/\n/g, "\\n").replace(/\t/g, "\\t");
}

function renderTerminal() {
  elements.terminal.querySelectorAll(".terminal-row").forEach((row) => row.remove());
  elements.terminalPlaceholder.hidden = state.entries.length > 0;
  state.entries.forEach(appendTerminalRow);
}

function togglePause() {
  state.paused = !state.paused;
  elements.pauseButton.classList.toggle("active", state.paused);
  elements.pauseButton.textContent = state.paused ? "Folytatás" : "Szünet";
  elements.streamState.textContent = state.paused ? "MEGJELENÍTÉS SZÜNETEL" : state.status?.connected ? "ADATFOLYAM AKTÍV" : "ADATFOLYAM KÉSZ";
  if (!state.paused) renderTerminal();
}

function clearData() {
  state.entries = [];
  state.rateWindow = [];
  elements.terminal.querySelectorAll(".terminal-row").forEach((row) => row.remove());
  elements.terminalPlaceholder.hidden = false;
  chart.clear();
  showToast("A helyi nézet törölve.");
}

function exportData() {
  if (!state.entries.length) {
    showToast("Még nincs exportálható adat.", true);
    return;
  }
  const blob = new Blob([JSON.stringify(state.entries, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `gree-usb-${new Date().toISOString().replaceAll(":", "-")}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

function trimRateWindow() {
  const cutoff = performance.now() - 5000;
  while (state.rateWindow.length && state.rateWindow[0].time < cutoff) state.rateWindow.shift();
}

function updateRate() {
  trimRateWindow();
  const total = state.rateWindow.reduce((sum, item) => sum + item.bytes, 0);
  elements.dataRate.textContent = `${formatBytes(total / 5)}/s`;
}

function updateLastSeen(isoTime) {
  const elapsed = Math.max(0, Date.now() - new Date(isoTime).getTime());
  elements.lastSeen.textContent = elapsed < 1500 ? "most" : elapsed < 60_000 ? `${Math.floor(elapsed / 1000)} mp` : new Date(isoTime).toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit" });
  elements.lastSeen.dataset.time = isoTime;
}

function formatInteger(value) {
  return new Intl.NumberFormat("hu-HU").format(value);
}

function formatBytes(bytes) {
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  const units = ["kB", "MB", "GB"];
  let value = bytes / 1000;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit += 1; }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

class SignalChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.series = new Map();
    this.observer = new ResizeObserver(() => this.draw());
    this.observer.observe(canvas.parentElement);
  }

  add(reading) {
    const values = Object.entries(reading.values || {});
    if (!values.length) return;
    const timestamp = new Date(reading.receivedAt).getTime();
    values.forEach(([name, value]) => {
      if (!this.series.has(name) && this.series.size >= COLORS.length) return;
      if (!this.series.has(name)) this.series.set(name, []);
      const points = this.series.get(name);
      points.push({ time: timestamp, value: Number(value) });
      if (points.length > 90) points.shift();
    });
    elements.chartEmpty.hidden = true;
    this.renderLegend();
    this.draw();
  }

  clear() {
    this.series.clear();
    elements.chartEmpty.hidden = false;
    elements.chartLegend.replaceChildren();
    this.draw();
  }

  renderLegend() {
    elements.chartLegend.replaceChildren();
    [...this.series.entries()].forEach(([name, points], index) => {
      const label = document.createElement("span");
      label.innerHTML = `<i style="background:${COLORS[index]}"></i>`;
      label.append(`${name}: ${formatChartValue(points.at(-1).value)}`);
      elements.chartLegend.append(label);
    });
  }

  draw() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, rect.width);
    const height = Math.max(1, rect.height);
    this.canvas.width = width * dpr;
    this.canvas.height = height * dpr;
    this.context.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.context.clearRect(0, 0, width, height);
    this.drawGrid(width, height);
    if (!this.series.size) return;

    const allPoints = [...this.series.values()].flat();
    const start = Math.min(...allPoints.map((point) => point.time));
    const end = Math.max(...allPoints.map((point) => point.time), start + 1000);
    const inset = { left: 48, right: 18, top: 20, bottom: 28 };
    const chartWidth = width - inset.left - inset.right;
    const chartHeight = height - inset.top - inset.bottom;

    this.context.font = "9px SFMono-Regular, Consolas, monospace";
    this.context.fillStyle = "#66746d";
    this.context.textAlign = "right";
    for (let row = 0; row <= 4; row += 1) {
      this.context.fillText(`${100 - row * 25}%`, inset.left - 9, inset.top + (chartHeight * row) / 4 + 3);
    }

    [...this.series.entries()].forEach(([, points], index) => {
      if (!points.length) return;
      const values = points.map((point) => point.value);
      let minimum = Math.min(...values);
      let maximum = Math.max(...values);
      if (maximum === minimum) { maximum += 1; minimum -= 1; }
      const padding = (maximum - minimum) * 0.12;
      maximum += padding;
      minimum -= padding;
      this.context.beginPath();
      this.context.strokeStyle = COLORS[index];
      this.context.lineWidth = 1.7;
      this.context.lineJoin = "round";
      points.forEach((point, pointIndex) => {
        const x = inset.left + ((point.time - start) / (end - start)) * chartWidth;
        const y = inset.top + ((maximum - point.value) / (maximum - minimum)) * chartHeight;
        if (pointIndex === 0) this.context.moveTo(x, y);
        else this.context.lineTo(x, y);
      });
      this.context.stroke();

      const last = points.at(-1);
      const x = inset.left + ((last.time - start) / (end - start)) * chartWidth;
      const y = inset.top + ((maximum - last.value) / (maximum - minimum)) * chartHeight;
      this.context.beginPath();
      this.context.fillStyle = COLORS[index];
      this.context.arc(x, y, 3.2, 0, Math.PI * 2);
      this.context.fill();
    });
  }

  drawGrid(width, height) {
    const inset = { left: 48, right: 18, top: 20, bottom: 28 };
    const chartWidth = width - inset.left - inset.right;
    const chartHeight = height - inset.top - inset.bottom;
    this.context.strokeStyle = "rgba(218, 237, 226, 0.07)";
    this.context.lineWidth = 1;
    for (let row = 0; row <= 4; row += 1) {
      const y = inset.top + (chartHeight * row) / 4;
      this.context.beginPath(); this.context.moveTo(inset.left, y); this.context.lineTo(width - inset.right, y); this.context.stroke();
    }
    for (let column = 0; column <= 5; column += 1) {
      const x = inset.left + (chartWidth * column) / 5;
      this.context.beginPath(); this.context.moveTo(x, inset.top); this.context.lineTo(x, height - inset.bottom); this.context.stroke();
    }
  }
}

function formatChartValue(value) {
  const absolute = Math.abs(value);
  if ((absolute >= 10_000 || (absolute > 0 && absolute < 0.01))) return value.toExponential(1);
  return new Intl.NumberFormat("hu-HU", { maximumFractionDigits: 2 }).format(value);
}

const chart = new SignalChart(elements.canvas);

elements.connectionForm.addEventListener("submit", connectSerial);
elements.demoButton.addEventListener("click", startDemo);
elements.disconnectButton.addEventListener("click", disconnect);
elements.refreshPorts.addEventListener("click", loadPorts);
elements.pauseButton.addEventListener("click", togglePause);
elements.clearButton.addEventListener("click", clearData);
elements.exportButton.addEventListener("click", exportData);
elements.climateScan.addEventListener("click", scanClimate);
elements.climateForm.addEventListener("submit", queryClimate);
elements.climateProfile.addEventListener("change", selectClimateProfile);
elements.climateProfileSave.addEventListener("click", saveClimateProfile);
elements.climateProfileDelete.addEventListener("click", deleteClimateProfile);
$$('[data-format]').forEach((button) => button.addEventListener("click", () => {
  state.format = button.dataset.format;
  $$('[data-format]').forEach((candidate) => candidate.classList.toggle("active", candidate === button));
  renderTerminal();
}));

setInterval(updateRate, 1000);
setInterval(() => {
  if (elements.lastSeen.dataset.time) updateLastSeen(elements.lastSeen.dataset.time);
}, 1000);
setInterval(loadTapHomeStatus, 5000);

Promise.all([
  loadPorts(),
  loadClimateOverview(),
  loadClimateProfiles(),
  loadTapHomeStatus(),
  api("/api/status").then(applyStatus).catch((error) => setFormError(error.message)),
]);
connectEventStream();
