(() => {
  const $ = (id) => document.getElementById(id);
  const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });

  function setConnection(mode, label) {
    const element = $("connection");
    if (!element) return;
    element.className = `connection ${mode}`;
    if (element.lastElementChild) element.lastElementChild.textContent = label;
  }

  function formatValue(value, digits = 4) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "—";
    return Number(value).toPrecision(digits);
  }

  function formatSeconds(value) {
    return Number.isFinite(value) ? `${number.format(value)} s` : "—";
  }

  function formatTime(value) {
    if (!Number.isFinite(value)) return "—";
    const minutes = Math.floor(Math.max(0, value) / 60);
    const seconds = Math.max(0, value) - minutes * 60;
    return minutes ? `${minutes}:${seconds.toFixed(1).padStart(4, "0")}` : `${seconds.toFixed(2)}s`;
  }

  function optionElement(value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  function selectedRecording() {
    return new URLSearchParams(window.location.search).get("recording");
  }

  function withRecording(url) {
    const target = new URL(url, window.location.origin);
    const recording = selectedRecording();
    if (recording && target.pathname.startsWith("/api/") && target.pathname !== "/api/recordings") {
      target.searchParams.set("recording", recording);
    }
    return `${target.pathname}${target.search}${target.hash}`;
  }

  function pageURL(path, parameters = {}) {
    const target = new URL(path, window.location.origin);
    const recording = selectedRecording();
    if (recording) target.searchParams.set("recording", recording);
    Object.entries(parameters).forEach(([name, value]) => {
      if (value === null || value === undefined || value === "") target.searchParams.delete(name);
      else target.searchParams.set(name, value);
    });
    return `${target.pathname}${target.search}${target.hash}`;
  }

  async function getJSON(url) {
    const response = await fetch(withRecording(url), { cache: "no-store" });
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new Error(`服务器返回异常（HTTP ${response.status}）`); }
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function canvasContext(id) {
    const canvas = $(id);
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.floor(rect.width * ratio);
    canvas.height = Math.floor(rect.height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    return { canvas, ctx, width: rect.width, height: rect.height };
  }

  function chartTheme() {
    const styles = getComputedStyle(document.documentElement);
    const color = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
    return {
      background: color("--chart-bg", "#0a1117"),
      grid: color("--chart-grid", "rgba(167, 224, 203, 0.10)"),
      text: color("--chart-text", "#79968b"),
      axis: color("--chart-axis", "rgba(241, 245, 249, 0.35)"),
      stimulusFill: color("--chart-stimulus-fill", "rgba(45, 212, 191, 0.10)"),
      motionFill: color("--chart-motion-fill", "rgba(255, 92, 92, 0.11)"),
      eventFill: color("--chart-event-fill", "rgba(245, 185, 66, 0.055)"),
      eventStroke: color("--chart-event-stroke", "rgba(245, 185, 66, 0.32)"),
      eventText: color("--chart-event-text", "rgba(245, 185, 66, 0.8)"),
      selectionFill: color("--chart-selection-fill", "rgba(45, 212, 191, 0.12)"),
      selectionStroke: color("--chart-selection-stroke", "rgba(45, 212, 191, 0.7)"),
      hover: color("--chart-hover", "rgba(241, 245, 249, 0.42)"),
      hbo: color("--hbo", "#ff765f"),
      hbr: color("--hbr", "#59a8ff"),
    };
  }

  function drawAxes(ctx, width, height, bounds, xFormatter) {
    const pad = { left: 68, right: 22, top: 26, bottom: 42 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;
    const x = (value) => pad.left + ((value - bounds.xmin) / Math.max(bounds.xmax - bounds.xmin, 1e-12)) * plotWidth;
    const y = (value) => pad.top + (1 - (value - bounds.ymin) / Math.max(bounds.ymax - bounds.ymin, Number.EPSILON)) * plotHeight;
    const theme = chartTheme();
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = theme.background;
    ctx.fillRect(0, 0, width, height);
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillStyle = theme.text;
    ctx.strokeStyle = theme.grid;
    ctx.lineWidth = 1;
    for (let index = 0; index <= 5; index += 1) {
      const gy = pad.top + (index / 5) * plotHeight;
      ctx.beginPath(); ctx.moveTo(pad.left, gy); ctx.lineTo(width - pad.right, gy); ctx.stroke();
      ctx.fillText(formatValue(bounds.ymax - (index / 5) * (bounds.ymax - bounds.ymin), 3), 7, gy + 4);
    }
    for (let index = 0; index <= 6; index += 1) {
      const value = bounds.xmin + (index / 6) * (bounds.xmax - bounds.xmin);
      const gx = x(value);
      ctx.beginPath(); ctx.moveTo(gx, pad.top); ctx.lineTo(gx, height - pad.bottom); ctx.stroke();
      ctx.fillText(xFormatter(value), gx - 13, height - 17);
    }
    return { pad, x, y, plotWidth, plotHeight, theme };
  }

  function initShell(page) {
    document.querySelectorAll(".brand, [data-nav-page], [data-recording-link]").forEach((link) => {
      const href = link.getAttribute("href");
      if (href?.startsWith("/")) link.href = pageURL(href);
    });
    document.querySelectorAll("[data-nav-page]").forEach((link) => {
      const active = link.dataset.navPage === page;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
    });
    const refresh = $("refresh");
    if (refresh) refresh.addEventListener("click", () => window.location.reload());
  }

  function renderPageContext(payload) {
    const summary = payload.summary;
    if ($("context-file")) $("context-file").textContent = summary.filename;
    if ($("context-meta")) $("context-meta").textContent = `${number.format(summary.duration_seconds / 60)} min · ${summary.channels} 通道 · ${summary.sample_rate_hz ? number.format(summary.sample_rate_hz) : "—"} Hz`;
    if ($("updated-at")) $("updated-at").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
  }

  window.Dashboard = { $, number, setConnection, formatValue, formatSeconds, formatTime, optionElement, selectedRecording, withRecording, pageURL, getJSON, canvasContext, drawAxes, initShell, renderPageContext };
})();
