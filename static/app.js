const $ = (id) => document.getElementById(id);

const state = {
  recording: null,
  quality: [],
  signal: null,
  signals: [],
  taskResponse: null,
  currentKind: "conc_filtered",
  signalRequest: 0,
  viewRange: null,
  hoverTime: null,
  dragStartX: null,
  dragEndX: null,
  chartGeometry: null,
};
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });

function setConnection(mode, text) {
  const element = $("connection");
  element.className = `connection ${mode}`;
  element.lastElementChild.textContent = text;
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

function nearestPoint(points, time) {
  if (!points.length) return null;
  let low = 0;
  let high = points.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (points[middle][0] < time) low = middle + 1;
    else high = middle;
  }
  if (low > 0 && Math.abs(points[low - 1][0] - time) < Math.abs(points[low][0] - time)) return points[low - 1];
  return points[low];
}

function optionElement(value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  return option;
}

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    throw new Error(`服务器返回异常（HTTP ${response.status}）`);
  }
  if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function seriesOption() {
  return state.recording?.series_options.find((item) => item.kind === state.currentKind);
}

function populateComponents() {
  const series = seriesOption();
  if (!series) return;
  const select = $("component-select");
  const oldValue = select.value;
  const isConcentration = state.currentKind === "conc" || state.currentKind === "conc_filtered";
  const options = series.components.map((item) => optionElement(item.value, item.label));
  if (isConcentration && series.components.length > 1) {
    select.replaceChildren(optionElement("both", "HbO + HbR"), ...options);
    select.value = oldValue === "HbO" || oldValue === "HbR" ? oldValue : "both";
  } else {
    select.replaceChildren(...options);
    if (series.components.some((item) => item.value === oldValue)) select.value = oldValue;
  }
  select.disabled = !series.components.length;
  $("chart-title").textContent = series.label;
  document.querySelectorAll("#series-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.kind === state.currentKind);
  });
}

function renderEvents(payload) {
  $("event-chips").replaceChildren(...payload.event_counts.map((item) => {
    const chip = document.createElement("span");
    chip.className = "badge badge-neutral";
    chip.textContent = `${item.label} · ${item.count} 次`;
    return chip;
  }));
  $("event-list").replaceChildren(...payload.event_counts.map((item) => {
    const card = document.createElement("article");
    card.className = "event-card";
    const title = document.createElement("strong");
    title.textContent = item.label;
    const count = document.createElement("span");
    count.textContent = `${item.count} 个事件`;
    const line = document.createElement("i");
    card.append(title, count, line);
    return card;
  }));
}

function renderMetadata(summary) {
  const validation = summary.input_validation || {};
  const geometry = validation.geometry || {};
  const distance = geometry.distance_mm || {};
  const parameters = summary.analysis_parameters || {};
  const formatNumber = (value) => Number.isFinite(value) ? number.format(value) : "—";
  const warningText = validation.warnings?.length ? `需注意：${validation.warnings.join("；")}` : "通过";
  const rows = [
    ["文件", summary.filename],
    ["文件大小", `${(summary.file_size_bytes / 1024 / 1024).toFixed(2)} MiB`],
    ["采样点", number.format(summary.samples)],
    ["采样率", `${number.format(summary.sample_rate_hz)} Hz`],
    ["记录时长", `${number.format(summary.duration_seconds)} s`],
    ["通道 / 测量", `${summary.channels} / ${summary.measurements}`],
    ["波长", `${summary.wavelengths_nm.join(" / ")} nm`],
    ["Cedalion", summary.cedalion_version],
    ["分析参数", `DPF ${formatNumber(parameters.dpf)} · ${parameters.filter_hz?.join("–") || "—"} Hz · SNR ${formatNumber(parameters.snr_threshold)}`],
    ["输入校验", warningText],
    ["探头距离", `${formatNumber(distance.minimum)}–${formatNumber(distance.maximum)} mm · 中位数 ${formatNumber(distance.median)}`],
  ];
  $("metadata-list").replaceChildren(...rows.map(([key, value]) => {
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = value;
    row.append(dt, dd);
    return row;
  }));
}

function populateSummary(payload) {
  const summary = payload.summary;
  $("filename").textContent = summary.filename;
  $("engine-badge").textContent = `Cedalion ${summary.cedalion_version}`;
  $("dataset-detail").textContent = `${(summary.file_size_bytes / 1024 / 1024).toFixed(1)} MiB · ${summary.wavelengths_nm.join(" / ")} nm · DPF ${summary.dpf}`;
  $("samples").textContent = number.format(summary.samples);
  $("channels").textContent = number.format(summary.channels);
  $("sample-rate").textContent = summary.sample_rate_hz ? `${number.format(summary.sample_rate_hz)} Hz` : "未知";
  $("duration").textContent = `${number.format(summary.duration_seconds / 60)} min`;
  $("events").textContent = number.format(summary.stimulus_events);
  $("wavelengths").textContent = `${summary.wavelengths_nm.join(" / ")} nm · ${summary.measurements} 个测量`;
  $("quality-pass").textContent = `${payload.quality_summary.passed_channels} / ${payload.quality_summary.total_channels}`;
  document.querySelectorAll(".overview-grid .stat-card").forEach((card) => card.classList.remove("loading"));
  renderEvents(payload);
  renderMetadata(summary);

  const channelSelect = $("channel-select");
  channelSelect.replaceChildren(...payload.channels.map((item) => optionElement(
    item.index,
    `${item.label} · ${item.source}–${item.detector}`,
  )));
  channelSelect.disabled = false;
  populateComponents();
}

function updateQualityCard(summary) {
  const rate = summary.total_channels ? summary.passed_channels / summary.total_channels : 0;
  const good = rate >= 0.7;
  $("quality-card-status").className = `badge ${good ? "badge-good" : "badge-warning"}`;
  $("quality-card-status").textContent = good ? "质量良好" : "需要关注";
  $("quality-card-detail").textContent = `最低 SNR ≥ ${summary.snr_threshold} · 平均 SCI ≥ ${summary.sci_threshold}`;
  $("quality-progress").style.width = `${Math.round(rate * 100)}%`;
  $("quality-progress").style.background = good ? "var(--accent)" : "var(--warning)";
  $("quality-card").classList.toggle("warning", !good);
}

function filteredQuality() {
  const query = $("quality-search").value.trim().toLowerCase();
  const attentionOnly = $("attention-only").checked;
  return state.quality.filter((channel) => {
    const matches = !query || [channel.label, channel.source, channel.detector]
      .some((value) => String(value).toLowerCase().includes(query));
    return matches && (!attentionOnly || !channel.passed);
  });
}

function renderQualityTable() {
  const labels = ["通道", "光源", "探测器", "平均 / 最低 SNR", "平均 SCI", "判定"];
  const rows = filteredQuality().map((channel) => {
    const row = document.createElement("tr");
    row.classList.toggle("selected", Number($("channel-select").value) === channel.index);
    const values = [
      channel.label,
      channel.source,
      channel.detector,
      `${formatValue(channel.snr, 3)} / ${formatValue(channel.snr_minimum, 3)}`,
      formatValue(channel.sci, 3),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.dataset.label = labels[index];
      cell.textContent = value;
      row.appendChild(cell);
    });
    const cell = document.createElement("td");
    cell.dataset.label = labels[5];
    const status = document.createElement("span");
    status.className = `quality-status ${channel.passed ? "good" : "bad"}`;
    status.textContent = channel.passed ? "● 通过" : "● 需关注";
    cell.appendChild(status);
    row.appendChild(cell);
    row.addEventListener("click", () => {
      $("channel-select").value = channel.index;
      updateChannelInspector();
      renderQualityTable();
      loadSignal();
    });
    return row;
  });
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.className = "empty-cell";
    cell.colSpan = 6;
    cell.textContent = "没有符合筛选条件的通道";
    row.appendChild(cell);
    rows.push(row);
  }
  $("quality-body").replaceChildren(...rows);
}

function updateChannelInspector() {
  const index = Number($("channel-select").value);
  const channel = state.quality.find((item) => item.index === index);
  if (!channel) return;
  $("source-name").textContent = channel.source;
  $("detector-name").textContent = channel.detector;
  $("channel-snr").textContent = `${formatValue(channel.snr, 3)} / ${formatValue(channel.snr_minimum, 3)}`;
  $("channel-sci").textContent = formatValue(channel.sci, 3);
  $("snr-meter").style.width = `${Math.min(100, Math.max(0, (channel.snr_minimum || 0) / 4 * 100))}%`;
  $("sci-meter").style.width = `${Math.min(100, Math.max(0, (channel.sci || 0) * 100))}%`;
  $("snr-meter").style.background = channel.snr_minimum >= 2 ? "var(--accent)" : "var(--danger)";
  $("sci-meter").style.background = channel.sci >= 0.7 ? "var(--accent)" : "var(--danger)";
  $("channel-status").className = `badge ${channel.passed ? "badge-good" : "badge-bad"}`;
  $("channel-status").textContent = channel.passed ? "质量通过" : "需关注";
}

async function loadQuality() {
  const payload = await getJSON("/api/quality");
  state.quality = payload.channels;
  const summary = payload.summary;
  $("quality-summary").textContent = `${summary.passed_channels} / ${summary.total_channels} 个通道通过`;
  updateQualityCard(summary);
  const current = Number($("channel-select").value);
  $("channel-select").replaceChildren(...state.quality.map((channel) => optionElement(
    channel.index,
    `${channel.label} · ${channel.source}–${channel.detector}${channel.passed ? "" : " · 需关注"}`,
  )));
  const currentChannel = state.quality.find((item) => item.index === current);
  const preferredChannel = currentChannel?.passed
    ? currentChannel
    : state.quality.find((item) => item.passed) || currentChannel;
  if (preferredChannel) $("channel-select").value = preferredChannel.index;
  updateChannelInspector();
  renderQualityTable();
}

async function loadSignal() {
  if (!state.recording) return;
  const requestId = ++state.signalRequest;
  $("chart-message").classList.remove("hidden");
  $("chart-message").textContent = "正在获取分析曲线…";
  const series = seriesOption();
  const selectedComponent = $("component-select").value;
  const components = selectedComponent === "both"
    ? series.components.map((item) => item.value)
    : [selectedComponent];
  try {
    const signals = await Promise.all(components.map((component) => {
      const query = new URLSearchParams({
        kind: state.currentKind,
        component,
        channel: $("channel-select").value,
        max_points: "2400",
      });
      return getJSON(`/api/signal?${query}`);
    }));
    if (requestId !== state.signalRequest) return;
    state.signals = signals;
    state.signal = signals[0];
    state.viewRange = null;
    state.hoverTime = null;
    const payload = state.signal;
    const unit = payload.series.unit;
    const componentLabel = signals.map((item) => item.series.component).join(" + ");
    $("chart-title").textContent = `${payload.series.label} · ${componentLabel}`;
    $("chart-subtitle").textContent = `${payload.series.channel.label}（${payload.series.channel.source}–${payload.series.channel.detector}）· 单位 ${unit}`;
    $("stats-component").textContent = `${payload.series.component} · ${unit}`;
    $("stat-min").textContent = `${formatValue(payload.stats.minimum)} ${unit}`;
    $("stat-max").textContent = `${formatValue(payload.stats.maximum)} ${unit}`;
    $("stat-mean").textContent = `${formatValue(payload.stats.mean)} ${unit}`;
    $("stat-std").textContent = `${formatValue(payload.stats.stddev)} ${unit}`;
    const stride = Math.max(...signals.map((item) => item.stride));
    $("downsample-note").textContent = stride > 1
      ? `每 ${stride} 点显示 1 点，统计基于完整数据`
      : "显示全部采样点";
    $("chart-legend").replaceChildren(...signals.map((item) => {
      const legend = document.createElement("span");
      legend.className = "legend-item";
      const line = document.createElement("i");
      line.style.background = lineColor(item)[0];
      legend.append(line, document.createTextNode(item.series.component));
      return legend;
    }));
    $("chart-message").classList.add("hidden");
    $("reset-zoom").disabled = true;
    drawSignalChart();
  } catch (error) {
    if (requestId !== state.signalRequest) return;
    $("chart-message").textContent = error.message;
  }
}

function lineColor(signal = state.signal) {
  const component = signal?.series?.component;
  const kind = signal?.series?.kind;
  if (component === "HbO") return ["#ff765f", "#ffb071"];
  if (component === "HbR") return ["#59a8ff", "#73e0ef"];
  if (kind === "od") return ["#f3c770", "#ff9d66"];
  return ["#5fe0b0", "#67a9ff"];
}

function canvasContext(id) {
  const canvas = $(id);
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  return { canvas, ctx, width: rect.width, height: rect.height };
}

function drawAxes(ctx, width, height, bounds, xFormatter) {
  const pad = { left: 68, right: 22, top: 26, bottom: 42 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const x = (value) => pad.left + ((value - bounds.xmin) / Math.max(bounds.xmax - bounds.xmin, 1e-12)) * plotWidth;
  const y = (value) => pad.top + (1 - (value - bounds.ymin) / Math.max(bounds.ymax - bounds.ymin, Number.EPSILON)) * plotHeight;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#0a1117";
  ctx.fillRect(0, 0, width, height);
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillStyle = "#79968b";
  ctx.strokeStyle = "rgba(167, 224, 203, 0.10)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i += 1) {
    const gy = pad.top + (i / 5) * plotHeight;
    ctx.beginPath(); ctx.moveTo(pad.left, gy); ctx.lineTo(width - pad.right, gy); ctx.stroke();
    ctx.fillText(formatValue(bounds.ymax - (i / 5) * (bounds.ymax - bounds.ymin), 3), 7, gy + 4);
  }
  for (let i = 0; i <= 6; i += 1) {
    const value = bounds.xmin + (i / 6) * (bounds.xmax - bounds.xmin);
    const gx = x(value);
    ctx.beginPath(); ctx.moveTo(gx, pad.top); ctx.lineTo(gx, height - pad.bottom); ctx.stroke();
    ctx.fillText(xFormatter(value), gx - 13, height - 17);
  }
  return { pad, x, y, plotWidth, plotHeight };
}

function drawSignalChart() {
  if (!state.signals.length) return;
  const surface = canvasContext("signal-chart");
  if (!surface) return;
  const allPoints = state.signals.flatMap((signal) => signal.points).filter((point) => point[1] !== null);
  if (!allPoints.length) return;
  const allTimes = allPoints.map((point) => point[0]);
  const fullMin = Math.min(...allTimes);
  const fullMax = Math.max(...allTimes);
  const xmin = state.viewRange ? Math.max(fullMin, state.viewRange[0]) : fullMin;
  const xmax = state.viewRange ? Math.min(fullMax, state.viewRange[1]) : fullMax;
  const visible = state.signals.map((signal) => ({
    signal,
    points: signal.points.filter((point) => point[1] !== null && point[0] >= xmin && point[0] <= xmax),
  }));
  const ys = visible.flatMap((item) => item.points.map((point) => point[1]));
  if (!ys.length) return;
  let ymin = Math.min(...ys);
  let ymax = Math.max(...ys);
  const ypadding = Math.max((ymax - ymin) * 0.08, Math.abs(ymax || 1) * 0.005, 1e-12);
  ymin -= ypadding;
  ymax += ypadding;
  const axes = drawAxes(surface.ctx, surface.width, surface.height, {
    xmin, xmax, ymin, ymax,
  }, formatTime);
  state.chartGeometry = {
    ...axes,
    width: surface.width,
    height: surface.height,
    xmin,
    xmax,
    timeAtX: (value) => xmin + ((value - axes.pad.left) / axes.plotWidth) * (xmax - xmin),
  };
  surface.ctx.fillText(state.signal.series.unit, 9, 16);

  (state.recording?.events || []).forEach((event) => {
    const onset = Number(event.onset);
    if (!Number.isFinite(onset) || onset < xmin || onset > xmax) return;
    const end = Math.min(xmax, onset + Math.max(0, Number(event.duration) || 0));
    const startX = axes.x(onset);
    const endX = axes.x(end);
    surface.ctx.fillStyle = "rgba(245,185,66,.055)";
    surface.ctx.fillRect(startX, axes.pad.top, Math.max(1, endX - startX), axes.plotHeight);
    surface.ctx.save();
    surface.ctx.setLineDash([3, 4]);
    surface.ctx.strokeStyle = "rgba(245,185,66,.32)";
    surface.ctx.beginPath();
    surface.ctx.moveTo(startX, axes.pad.top);
    surface.ctx.lineTo(startX, surface.height - axes.pad.bottom);
    surface.ctx.stroke();
    surface.ctx.restore();
  });

  visible.forEach(({ signal, points }) => {
    const colors = lineColor(signal);
    const gradient = surface.ctx.createLinearGradient(axes.pad.left, 0, surface.width - axes.pad.right, 0);
    gradient.addColorStop(0, colors[0]);
    gradient.addColorStop(1, colors[1]);
    surface.ctx.strokeStyle = gradient;
    surface.ctx.lineWidth = 1.6;
    surface.ctx.beginPath();
    points.forEach((point, index) => {
      if (index === 0) surface.ctx.moveTo(axes.x(point[0]), axes.y(point[1]));
      else surface.ctx.lineTo(axes.x(point[0]), axes.y(point[1]));
    });
    surface.ctx.stroke();
  });

  if (state.hoverTime !== null && state.dragStartX === null) {
    const hoverX = axes.x(state.hoverTime);
    surface.ctx.save();
    surface.ctx.setLineDash([3, 3]);
    surface.ctx.strokeStyle = "rgba(241,245,249,.42)";
    surface.ctx.beginPath();
    surface.ctx.moveTo(hoverX, axes.pad.top);
    surface.ctx.lineTo(hoverX, surface.height - axes.pad.bottom);
    surface.ctx.stroke();
    surface.ctx.restore();
    state.signals.forEach((signal) => {
      const point = nearestPoint(signal.points, state.hoverTime);
      if (!point || point[1] === null) return;
      surface.ctx.fillStyle = lineColor(signal)[0];
      surface.ctx.beginPath();
      surface.ctx.arc(axes.x(point[0]), axes.y(point[1]), 3.5, 0, Math.PI * 2);
      surface.ctx.fill();
    });
  }

  if (state.dragStartX !== null && state.dragEndX !== null) {
    const startX = Math.max(axes.pad.left, Math.min(state.dragStartX, state.dragEndX));
    const endX = Math.min(surface.width - axes.pad.right, Math.max(state.dragStartX, state.dragEndX));
    surface.ctx.fillStyle = "rgba(45,212,191,.12)";
    surface.ctx.fillRect(startX, axes.pad.top, Math.max(0, endX - startX), axes.plotHeight);
    surface.ctx.strokeStyle = "rgba(45,212,191,.7)";
    surface.ctx.strokeRect(startX, axes.pad.top, Math.max(0, endX - startX), axes.plotHeight);
  }
}

function signalEventAt(time) {
  return (state.recording?.events || []).find((event) => {
    const onset = Number(event.onset);
    const duration = Math.max(0.5, Number(event.duration) || 0);
    return time >= onset && time <= onset + duration;
  });
}

function showSignalTooltip(event, time) {
  const tooltip = $("chart-tooltip");
  const chartRect = $("chart-wrap").getBoundingClientRect();
  const title = document.createElement("div");
  title.className = "tooltip-time";
  title.textContent = `时间 ${formatTime(time)}`;
  const content = [title];
  state.signals.forEach((signal) => {
    const point = nearestPoint(signal.points, time);
    if (!point || point[1] === null) return;
    const row = document.createElement("div");
    row.className = "tooltip-row";
    const label = document.createElement("span");
    const dot = document.createElement("i");
    const value = document.createElement("strong");
    dot.style.background = lineColor(signal)[0];
    label.append(dot, document.createTextNode(signal.series.component));
    value.textContent = `${formatValue(point[1])} ${signal.series.unit}`;
    row.append(label, value);
    content.push(row);
  });
  const stimulus = signalEventAt(time);
  if (stimulus) {
    const note = document.createElement("div");
    note.className = "tooltip-event";
    note.textContent = `刺激：${stimulus.label}`;
    content.push(note);
  }
  tooltip.replaceChildren(...content);
  tooltip.hidden = false;
  const localX = event.clientX - chartRect.left;
  const localY = event.clientY - chartRect.top;
  const width = tooltip.offsetWidth;
  const height = tooltip.offsetHeight;
  tooltip.style.left = `${Math.max(8, Math.min(chartRect.width - width - 8, localX + 14))}px`;
  tooltip.style.top = `${Math.max(8, Math.min(chartRect.height - height - 8, localY - height / 2))}px`;
}

function resetSignalZoom() {
  state.viewRange = null;
  state.hoverTime = null;
  $("chart-tooltip").hidden = true;
  $("reset-zoom").disabled = true;
  drawSignalChart();
}

function setupTaskControls() {
  const task = state.recording.task;
  $("task-channels").textContent = `${task.usable_channels} / ${state.recording.summary.channels}`;
  $("task-excluded").textContent = `排除 ${task.excluded_channels} 个未过门限通道`;
  $("task-method").textContent = `${task.motion_correction} · ${task.filter_hz.join("–")} Hz · 基线 ${task.baseline_seconds.join(" 至 ")} 秒`;
  if (!task.available) {
    $("task-message").textContent = task.error || "任务分析不可用";
    return false;
  }
  const conditionSelect = $("task-condition");
  conditionSelect.replaceChildren(...task.conditions.map((item) => optionElement(item.value, `${item.label} · ${item.count} 次`)));
  if (task.conditions.some((item) => item.value === "Tapping/Left")) conditionSelect.value = "Tapping/Left";
  conditionSelect.disabled = false;
  const taskChannel = $("task-channel");
  const passed = state.quality.filter((item) => item.passed);
  taskChannel.replaceChildren(...passed.map((item) => optionElement(item.index, `${item.label} · ${item.source}–${item.detector}`)));
  taskChannel.disabled = !passed.length;
  return Boolean(passed.length);
}

async function loadTaskResponse() {
  if (!state.recording?.task?.available || $("task-channel").disabled) return;
  $("task-message").classList.remove("hidden");
  $("task-message").textContent = "正在获取分段平均结果…";
  const query = new URLSearchParams({
    condition: $("task-condition").value,
    channel: $("task-channel").value,
    max_points: "600",
  });
  try {
    const payload = await getJSON(`/api/task-response?${query}`);
    state.taskResponse = payload;
    $("task-title").textContent = `${payload.condition.label} · 任务诱发血氧响应`;
    $("task-subtitle").textContent = `${payload.channel.label}（${payload.channel.source}–${payload.channel.detector}）· 平均值 ± SEM`;
    $("task-epochs").textContent = `${payload.condition.count} 次`;
    $("task-epochs-detail").textContent = "刺激前 5 秒基线，刺激后观察 20 秒";
    $("hbo-peak").textContent = `${formatValue(payload.metrics.hbo_peak.amplitude)} µM`;
    $("hbo-latency").textContent = `峰值潜伏期 ${formatSeconds(payload.metrics.hbo_peak.latency_seconds)}`;
    $("hbr-trough").textContent = `${formatValue(payload.metrics.hbr_trough.amplitude)} µM`;
    $("hbr-latency").textContent = `谷值潜伏期 ${formatSeconds(payload.metrics.hbr_trough.latency_seconds)}`;
    $("task-message").classList.add("hidden");
    $("export").disabled = false;
    $("task-png").disabled = false;
    drawTaskChart();
  } catch (error) {
    $("task-message").textContent = error.message;
  }
}

function drawTaskChart() {
  if (!state.taskResponse?.series?.length) return;
  const surface = canvasContext("task-chart");
  if (!surface) return;
  const allPoints = state.taskResponse.series.flatMap((series) => series.points).filter((point) => point[1] !== null);
  if (!allPoints.length) return;
  const xs = allPoints.map((point) => point[0]);
  const ys = allPoints.flatMap((point) => {
    const sem = Number.isFinite(point[2]) ? point[2] : 0;
    return [point[1] - sem, point[1] + sem];
  });
  let ymin = Math.min(...ys, 0);
  let ymax = Math.max(...ys, 0);
  const margin = Math.max((ymax - ymin) * 0.1, 0.001);
  ymin -= margin; ymax += margin;
  const bounds = { xmin: Math.min(...xs), xmax: Math.max(...xs), ymin, ymax };
  const axes = drawAxes(surface.ctx, surface.width, surface.height, bounds, (value) => `${number.format(value)}s`);
  const stimulus = state.taskResponse.stimulus;
  surface.ctx.fillStyle = "rgba(45, 212, 191, 0.10)";
  surface.ctx.fillRect(
    axes.x(stimulus.onset_seconds),
    axes.pad.top,
    axes.x(stimulus.onset_seconds + stimulus.duration_seconds) - axes.x(stimulus.onset_seconds),
    axes.plotHeight,
  );
  surface.ctx.save();
  surface.ctx.setLineDash([4, 4]);
  surface.ctx.strokeStyle = "rgba(241,245,249,.35)";
  surface.ctx.beginPath(); surface.ctx.moveTo(axes.pad.left, axes.y(0)); surface.ctx.lineTo(surface.width - axes.pad.right, axes.y(0)); surface.ctx.stroke();
  surface.ctx.restore();
  surface.ctx.fillStyle = "#79968b";
  surface.ctx.fillText("µM", 9, 16);

  const colors = { HbO: "#ff765f", HbR: "#59a8ff" };
  state.taskResponse.series.forEach((series) => {
    const points = series.points.filter((point) => point[1] !== null);
    surface.ctx.fillStyle = `${colors[series.name]}24`;
    surface.ctx.beginPath();
    points.forEach((point, index) => {
      const sem = Number.isFinite(point[2]) ? point[2] : 0;
      const px = axes.x(point[0]); const py = axes.y(point[1] + sem);
      if (index === 0) surface.ctx.moveTo(px, py); else surface.ctx.lineTo(px, py);
    });
    [...points].reverse().forEach((point) => {
      const sem = Number.isFinite(point[2]) ? point[2] : 0;
      surface.ctx.lineTo(axes.x(point[0]), axes.y(point[1] - sem));
    });
    surface.ctx.closePath(); surface.ctx.fill();
    surface.ctx.strokeStyle = colors[series.name];
    surface.ctx.lineWidth = 2;
    surface.ctx.beginPath();
    points.forEach((point, index) => {
      if (index === 0) surface.ctx.moveTo(axes.x(point[0]), axes.y(point[1]));
      else surface.ctx.lineTo(axes.x(point[0]), axes.y(point[1]));
    });
    surface.ctx.stroke();
  });
}

function taskQuery() {
  return new URLSearchParams({
    condition: $("task-condition").value,
    channel: $("task-channel").value,
  });
}

function exportTaskCSV() {
  if (!state.taskResponse) return;
  const link = document.createElement("a");
  link.href = `/api/task-export?${taskQuery()}`;
  link.click();
}

function exportTaskPNG() {
  if (!state.taskResponse) return;
  $("task-chart").toBlob((blob) => {
    if (!blob) return;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `task-${state.taskResponse.condition.value.replace("/", "-")}-${state.taskResponse.channel.label}.png`;
    link.click();
    URL.revokeObjectURL(link.href);
  }, "image/png");
}

async function loadRecording() {
  const refresh = $("refresh");
  refresh.disabled = true;
  refresh.classList.add("busy");
  setConnection("waiting", "Cedalion 正在分析");
  $("chart-message").classList.remove("hidden");
  $("chart-message").textContent = "服务器正在执行 OD、HbO/HbR、质量与 TDDR 任务分析…";
  $("task-message").classList.remove("hidden");
  $("task-message").textContent = "服务器正在执行 TDDR 与任务分段平均，首次约需 10–20 秒…";
  try {
    const payload = await getJSON("/api/recording");
    state.recording = payload;
    populateSummary(payload);
    await loadQuality();
    setupTaskControls();
    setConnection("online", `Cedalion ${payload.summary.cedalion_version} 已连接`);
    $("updated-at").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
    await Promise.all([loadSignal(), loadTaskResponse()]);
  } catch (error) {
    setConnection("error", "分析服务异常");
    $("chart-message").textContent = error.message;
    $("task-message").textContent = error.message;
  } finally {
    refresh.disabled = false;
    refresh.classList.remove("busy");
  }
}

document.querySelectorAll("#series-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    state.currentKind = button.dataset.kind;
    populateComponents();
    loadSignal();
  });
});
$("component-select").addEventListener("change", loadSignal);
$("channel-select").addEventListener("change", () => {
  updateChannelInspector();
  renderQualityTable();
  loadSignal();
});
$("task-condition").addEventListener("change", loadTaskResponse);
$("task-channel").addEventListener("change", loadTaskResponse);
$("quality-search").addEventListener("input", renderQualityTable);
$("attention-only").addEventListener("change", renderQualityTable);
$("refresh").addEventListener("click", loadRecording);
$("reset-zoom").addEventListener("click", resetSignalZoom);
$("export").addEventListener("click", exportTaskCSV);
$("task-png").addEventListener("click", exportTaskPNG);
document.querySelectorAll(".detail-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".detail-tabs button").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".detail-view").forEach((panel) => panel.classList.toggle("active", panel.id === button.dataset.panel));
  });
});

const signalCanvas = $("signal-chart");

function signalPointerPosition(event) {
  const rect = signalCanvas.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function insideSignalPlot(position) {
  const geometry = state.chartGeometry;
  return geometry
    && position.x >= geometry.pad.left
    && position.x <= geometry.width - geometry.pad.right
    && position.y >= geometry.pad.top
    && position.y <= geometry.height - geometry.pad.bottom;
}

signalCanvas.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || !state.chartGeometry) return;
  const position = signalPointerPosition(event);
  if (!insideSignalPlot(position)) return;
  signalCanvas.setPointerCapture(event.pointerId);
  state.dragStartX = position.x;
  state.dragEndX = position.x;
  state.hoverTime = null;
  $("chart-tooltip").hidden = true;
  drawSignalChart();
});

signalCanvas.addEventListener("pointermove", (event) => {
  const geometry = state.chartGeometry;
  if (!geometry) return;
  const position = signalPointerPosition(event);
  if (state.dragStartX !== null) {
    state.dragEndX = Math.max(geometry.pad.left, Math.min(geometry.width - geometry.pad.right, position.x));
    drawSignalChart();
    return;
  }
  if (!insideSignalPlot(position)) {
    state.hoverTime = null;
    $("chart-tooltip").hidden = true;
    drawSignalChart();
    return;
  }
  state.hoverTime = geometry.timeAtX(position.x);
  showSignalTooltip(event, state.hoverTime);
  drawSignalChart();
});

function finishSignalDrag(event) {
  const geometry = state.chartGeometry;
  if (!geometry || state.dragStartX === null) return;
  const start = state.dragStartX;
  const end = state.dragEndX ?? start;
  state.dragStartX = null;
  state.dragEndX = null;
  if (Math.abs(end - start) > 8) {
    state.viewRange = [
      geometry.timeAtX(Math.min(start, end)),
      geometry.timeAtX(Math.max(start, end)),
    ];
    $("reset-zoom").disabled = false;
  }
  try { signalCanvas.releasePointerCapture(event.pointerId); } catch (_) {}
  drawSignalChart();
}

signalCanvas.addEventListener("pointerup", finishSignalDrag);
signalCanvas.addEventListener("pointercancel", finishSignalDrag);
signalCanvas.addEventListener("pointerleave", () => {
  if (state.dragStartX !== null) return;
  state.hoverTime = null;
  $("chart-tooltip").hidden = true;
  drawSignalChart();
});
signalCanvas.addEventListener("dblclick", resetSignalZoom);

new ResizeObserver(() => { drawSignalChart(); drawTaskChart(); }).observe($("chart-wrap"));
new ResizeObserver(drawTaskChart).observe($("task-chart-wrap"));
loadRecording();
