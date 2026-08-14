(() => {
  const { $, setConnection, formatValue, formatTime, optionElement, getJSON, canvasContext, drawAxes, initShell, renderPageContext } = window.Dashboard;
  initShell("signals");

  const state = {
    recording: null,
    quality: [],
    signals: [],
    currentKind: "conc_filtered",
    signalRequest: 0,
    viewRange: null,
    hoverTime: null,
    dragStartX: null,
    dragEndX: null,
    chartGeometry: null,
  };

  function nearestPoint(points, time) {
    if (!points.length) return null;
    let low = 0; let high = points.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (points[middle][0] < time) low = middle + 1; else high = middle;
    }
    if (low > 0 && Math.abs(points[low - 1][0] - time) < Math.abs(points[low][0] - time)) return points[low - 1];
    return points[low];
  }

  function analysisKind() {
    return state.currentKind === "conc_filtered" ? $("motion-select").value : state.currentKind;
  }

  function seriesOption(kind = analysisKind()) {
    return state.recording?.series_options.find((item) => item.kind === kind);
  }

  function populateComponents() {
    const series = seriesOption();
    if (!series) return;
    const select = $("component-select");
    const previous = select.value;
    const concentration = state.currentKind === "conc" || state.currentKind === "conc_filtered";
    const options = series.components.map((item) => optionElement(item.value, item.label));
    if (concentration && series.components.length > 1) {
      select.replaceChildren(optionElement("both", "HbO + HbR"), ...options);
      select.value = previous === "HbO" || previous === "HbR" ? previous : "both";
    } else {
      select.replaceChildren(...options);
      if (series.components.some((item) => item.value === previous)) select.value = previous;
    }
    select.disabled = !series.components.length;
    $("motion-select").disabled = state.currentKind !== "conc_filtered";
    $("chart-title").textContent = series.label;
    document.querySelectorAll("#series-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.kind === state.currentKind));
  }

  function updateInspector() {
    const channel = state.quality.find((item) => item.index === Number($("channel-select").value));
    if (!channel) return;
    $("source-name").textContent = channel.source;
    $("detector-name").textContent = channel.detector;
    $("channel-snr").textContent = `${formatValue(channel.snr, 3)} / ${formatValue(channel.snr_minimum, 3)}`;
    $("channel-sci").textContent = formatValue(channel.sci, 3);
    $("channel-psp").textContent = formatValue(channel.psp, 4);
    $("channel-psp-note").textContent = channel.psp_clean_fraction == null
      ? "PSP 合格窗口比例不可用，通道已需关注"
      : `PSP 合格窗口 ${(channel.psp_clean_fraction * 100).toFixed(1)}% · ${channel.psp_clean_fraction >= 0.75 ? "通过" : "低于质量门"}`;
    $("snr-meter").style.width = `${Math.min(100, Math.max(0, (channel.snr_minimum || 0) / 4 * 100))}%`;
    $("sci-meter").style.width = `${Math.min(100, Math.max(0, (channel.sci || 0) * 100))}%`;
    $("snr-meter").style.background = channel.snr_minimum >= 2 ? "var(--accent)" : "var(--danger)";
    $("sci-meter").style.background = channel.sci >= 0.7 ? "var(--accent)" : "var(--danger)";
    $("channel-status").className = `badge ${channel.passed ? "badge-good" : "badge-bad"}`;
    $("channel-status").textContent = channel.passed ? "质量通过" : "需关注";
    const url = new URL(window.location.href);
    url.searchParams.set("channel", channel.index);
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  }

  function lineColor(signal) {
    const component = signal?.series?.component;
    const kind = signal?.series?.kind;
    if (component === "HbO") return ["#ff765f", "#ffb071"];
    if (component === "HbR") return ["#59a8ff", "#73e0ef"];
    if (kind === "od") return ["#f3c770", "#ff9d66"];
    return ["#5fe0b0", "#67a9ff"];
  }

  async function loadSignal() {
    if (!state.recording) return;
    const requestId = ++state.signalRequest;
    $("chart-message").classList.remove("hidden");
    $("chart-message").textContent = "正在获取分析曲线…";
    const series = seriesOption();
    const kind = analysisKind();
    const selectedComponent = $("component-select").value;
    const components = selectedComponent === "both" ? series.components.map((item) => item.value) : [selectedComponent];
    try {
      const signals = await Promise.all(components.map((component) => {
        const query = new URLSearchParams({ kind, component, channel: $("channel-select").value, max_points: "2400" });
        return getJSON(`/api/signal?${query}`);
      }));
      if (requestId !== state.signalRequest) return;
      state.signals = signals;
      state.viewRange = null;
      state.hoverTime = null;
      const first = signals[0];
      const unit = first.series.unit;
      const componentLabel = signals.map((item) => item.series.component).join(" + ");
      $("chart-title").textContent = `${first.series.label} · ${componentLabel}`;
      $("chart-subtitle").textContent = `${first.series.channel.label}（${first.series.channel.source}–${first.series.channel.detector}）· 单位 ${unit}`;
      $("stats-component").textContent = `${first.series.component} · ${unit}`;
      $("stat-min").textContent = `${formatValue(first.stats.minimum)} ${unit}`;
      $("stat-max").textContent = `${formatValue(first.stats.maximum)} ${unit}`;
      $("stat-mean").textContent = `${formatValue(first.stats.mean)} ${unit}`;
      $("stat-std").textContent = `${formatValue(first.stats.stddev)} ${unit}`;
      const stride = Math.max(...signals.map((item) => item.stride));
      $("downsample-note").textContent = stride > 1 ? `每 ${stride} 点显示 1 点，统计基于完整数据` : "显示全部采样点";
      const legendItems = signals.map((item) => {
        const legend = document.createElement("span"); legend.className = "legend-item";
        const line = document.createElement("i"); line.style.background = lineColor(item)[0];
        legend.append(line, document.createTextNode(item.series.component)); return legend;
      });
      if (state.recording.motion_segments?.length) {
        const legend = document.createElement("span"); legend.className = "legend-item";
        const line = document.createElement("i"); line.style.height = "8px"; line.style.background = "rgba(255,92,92,.45)";
        legend.append(line, document.createTextNode("GVTD 异常候选")); legendItems.push(legend);
      }
      $("chart-legend").replaceChildren(...legendItems);
      $("chart-message").classList.add("hidden");
      $("reset-zoom").disabled = true;
      drawChart();
    } catch (error) {
      if (requestId === state.signalRequest) $("chart-message").textContent = error.message;
    }
  }

  function drawChart() {
    if (!state.signals.length) return;
    const surface = canvasContext("signal-chart");
    if (!surface) return;
    const allPoints = state.signals.flatMap((signal) => signal.points).filter((point) => point[1] !== null);
    if (!allPoints.length) return;
    const times = allPoints.map((point) => point[0]);
    const fullMin = Math.min(...times); const fullMax = Math.max(...times);
    const xmin = state.viewRange ? Math.max(fullMin, state.viewRange[0]) : fullMin;
    const xmax = state.viewRange ? Math.min(fullMax, state.viewRange[1]) : fullMax;
    const visible = state.signals.map((signal) => ({ signal, points: signal.points.filter((point) => point[1] !== null && point[0] >= xmin && point[0] <= xmax) }));
    const values = visible.flatMap((item) => item.points.map((point) => point[1]));
    if (!values.length) return;
    let ymin = Math.min(...values); let ymax = Math.max(...values);
    const padding = Math.max((ymax - ymin) * 0.08, Math.abs(ymax || 1) * 0.005, 1e-12);
    ymin -= padding; ymax += padding;
    const axes = drawAxes(surface.ctx, surface.width, surface.height, { xmin, xmax, ymin, ymax }, formatTime);
    state.chartGeometry = { ...axes, width: surface.width, height: surface.height, xmin, xmax, timeAtX: (value) => xmin + ((value - axes.pad.left) / axes.plotWidth) * (xmax - xmin) };
    surface.ctx.fillText(state.signals[0].series.unit, 9, 16);

    (state.recording.motion_segments || []).forEach((segment) => {
      const onset = Number(segment.onset); const end = onset + Math.max(0, Number(segment.duration) || 0);
      if (!Number.isFinite(onset) || end < xmin || onset > xmax) return;
      const startX = axes.x(Math.max(xmin, onset)); const endX = axes.x(Math.min(xmax, end));
      surface.ctx.fillStyle = axes.theme.motionFill;
      surface.ctx.fillRect(startX, axes.pad.top, Math.max(2, endX - startX), axes.plotHeight);
    });

    const chartEvents = state.recording.intervals?.length
      ? state.recording.intervals
      : (state.recording.events || []);
    chartEvents.forEach((event) => {
      const onset = Number(event.onset);
      const duration = Math.max(0, Number(event.duration) || 0);
      const intervalEnd = onset + duration;
      if (!Number.isFinite(onset) || intervalEnd < xmin || onset > xmax) return;
      const visibleStart = Math.max(xmin, onset); const visibleEnd = Math.min(xmax, intervalEnd);
      const startX = axes.x(visibleStart); const endX = axes.x(visibleEnd);
      surface.ctx.fillStyle = axes.theme.eventFill;
      surface.ctx.fillRect(startX, axes.pad.top, Math.max(1, endX - startX), axes.plotHeight);
      surface.ctx.save(); surface.ctx.setLineDash([3, 4]); surface.ctx.strokeStyle = axes.theme.eventStroke;
      surface.ctx.beginPath(); surface.ctx.moveTo(startX, axes.pad.top); surface.ctx.lineTo(startX, surface.height - axes.pad.bottom); surface.ctx.stroke(); surface.ctx.restore();
      if (endX - startX > 24) {
        surface.ctx.fillStyle = axes.theme.eventText;
        surface.ctx.fillText(event.label, startX + 5, axes.pad.top + 13);
      }
    });

    visible.forEach(({ signal, points }) => {
      const colors = lineColor(signal);
      const gradient = surface.ctx.createLinearGradient(axes.pad.left, 0, surface.width - axes.pad.right, 0);
      gradient.addColorStop(0, colors[0]); gradient.addColorStop(1, colors[1]);
      surface.ctx.strokeStyle = gradient; surface.ctx.lineWidth = 1.6; surface.ctx.beginPath();
      points.forEach((point, index) => { if (!index) surface.ctx.moveTo(axes.x(point[0]), axes.y(point[1])); else surface.ctx.lineTo(axes.x(point[0]), axes.y(point[1])); });
      surface.ctx.stroke();
    });

    if (state.hoverTime !== null && state.dragStartX === null) {
      const hoverX = axes.x(state.hoverTime);
      surface.ctx.save(); surface.ctx.setLineDash([3, 3]); surface.ctx.strokeStyle = axes.theme.hover;
      surface.ctx.beginPath(); surface.ctx.moveTo(hoverX, axes.pad.top); surface.ctx.lineTo(hoverX, surface.height - axes.pad.bottom); surface.ctx.stroke(); surface.ctx.restore();
      state.signals.forEach((signal) => {
        const point = nearestPoint(signal.points, state.hoverTime);
        if (!point || point[1] === null) return;
        surface.ctx.fillStyle = lineColor(signal)[0]; surface.ctx.beginPath(); surface.ctx.arc(axes.x(point[0]), axes.y(point[1]), 3.5, 0, Math.PI * 2); surface.ctx.fill();
      });
    }
    if (state.dragStartX !== null && state.dragEndX !== null) {
      const startX = Math.max(axes.pad.left, Math.min(state.dragStartX, state.dragEndX));
      const endX = Math.min(surface.width - axes.pad.right, Math.max(state.dragStartX, state.dragEndX));
      surface.ctx.fillStyle = axes.theme.selectionFill; surface.ctx.fillRect(startX, axes.pad.top, Math.max(0, endX - startX), axes.plotHeight);
      surface.ctx.strokeStyle = axes.theme.selectionStroke; surface.ctx.strokeRect(startX, axes.pad.top, Math.max(0, endX - startX), axes.plotHeight);
    }
  }

  function eventAt(time) {
    const events = state.recording?.intervals?.length
      ? state.recording.intervals
      : (state.recording?.events || []);
    return events.find((event) => time >= Number(event.onset) && time <= Number(event.onset) + Math.max(0.5, Number(event.duration) || 0));
  }

  function motionAt(time) {
    return (state.recording?.motion_segments || []).find((segment) => time >= Number(segment.onset) && time <= Number(segment.onset) + Number(segment.duration));
  }

  function showTooltip(event, time) {
    const tooltip = $("chart-tooltip"); const chartRect = $("chart-wrap").getBoundingClientRect();
    const title = document.createElement("div"); title.className = "tooltip-time"; title.textContent = `时间 ${formatTime(time)}`;
    const content = [title];
    state.signals.forEach((signal) => {
      const point = nearestPoint(signal.points, time); if (!point || point[1] === null) return;
      const row = document.createElement("div"); row.className = "tooltip-row";
      const label = document.createElement("span"); const dot = document.createElement("i"); const value = document.createElement("strong");
      dot.style.background = lineColor(signal)[0]; label.append(dot, document.createTextNode(signal.series.component)); value.textContent = `${formatValue(point[1])} ${signal.series.unit}`;
      row.append(label, value); content.push(row);
    });
    const stimulus = eventAt(time);
    if (stimulus) { const note = document.createElement("div"); note.className = "tooltip-event"; note.textContent = `任务区间：${stimulus.label}`; content.push(note); }
    if (motionAt(time)) { const note = document.createElement("div"); note.className = "tooltip-event"; note.style.color = "#ff7c7c"; note.textContent = "GVTD：异常候选区段"; content.push(note); }
    tooltip.replaceChildren(...content); tooltip.hidden = false;
    const localX = event.clientX - chartRect.left; const localY = event.clientY - chartRect.top;
    tooltip.style.left = `${Math.max(8, Math.min(chartRect.width - tooltip.offsetWidth - 8, localX + 14))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(chartRect.height - tooltip.offsetHeight - 8, localY - tooltip.offsetHeight / 2))}px`;
  }

  function resetZoom() {
    state.viewRange = null; state.hoverTime = null; $("chart-tooltip").hidden = true; $("reset-zoom").disabled = true; drawChart();
  }

  async function load() {
    setConnection("waiting", "Cedalion 正在分析");
    try {
      const [recording, quality] = await Promise.all([getJSON("/api/recording"), getJSON("/api/quality")]);
      state.recording = recording; state.quality = quality.channels;
      renderPageContext(recording); populateComponents();
      const channelParameter = new URLSearchParams(window.location.search).get("channel");
      const requested = channelParameter === null ? Number.NaN : Number(channelParameter);
      const preferred = state.quality.find((item) => item.index === requested)
        || state.quality.find((item) => item.passed) || state.quality[0];
      $("channel-select").replaceChildren(...state.quality.map((channel) => optionElement(channel.index, `${channel.label} · ${channel.source}–${channel.detector}${channel.passed ? "" : " · 需关注"}`)));
      $("channel-select").disabled = !state.quality.length;
      if (preferred) $("channel-select").value = preferred.index;
      updateInspector();
      setConnection("online", `Cedalion ${recording.summary.cedalion_version} 已连接`);
      await loadSignal();
    } catch (error) {
      setConnection("error", "分析服务异常"); $("chart-message").textContent = error.message;
    }
  }

  document.querySelectorAll("#series-tabs button").forEach((button) => button.addEventListener("click", () => { state.currentKind = button.dataset.kind; populateComponents(); loadSignal(); }));
  $("component-select").addEventListener("change", loadSignal);
  $("motion-select").addEventListener("change", () => { populateComponents(); loadSignal(); });
  $("channel-select").addEventListener("change", () => { updateInspector(); loadSignal(); });
  $("reset-zoom").addEventListener("click", resetZoom);

  const canvas = $("signal-chart");
  const pointerPosition = (event) => { const rect = canvas.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; };
  const insidePlot = (position) => { const geometry = state.chartGeometry; return geometry && position.x >= geometry.pad.left && position.x <= geometry.width - geometry.pad.right && position.y >= geometry.pad.top && position.y <= geometry.height - geometry.pad.bottom; };
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !state.chartGeometry) return;
    const position = pointerPosition(event); if (!insidePlot(position)) return;
    canvas.setPointerCapture(event.pointerId); state.dragStartX = position.x; state.dragEndX = position.x; state.hoverTime = null; $("chart-tooltip").hidden = true; drawChart();
  });
  canvas.addEventListener("pointermove", (event) => {
    const geometry = state.chartGeometry; if (!geometry) return;
    const position = pointerPosition(event);
    if (state.dragStartX !== null) { state.dragEndX = Math.max(geometry.pad.left, Math.min(geometry.width - geometry.pad.right, position.x)); drawChart(); return; }
    if (!insidePlot(position)) { state.hoverTime = null; $("chart-tooltip").hidden = true; drawChart(); return; }
    state.hoverTime = geometry.timeAtX(position.x); showTooltip(event, state.hoverTime); drawChart();
  });
  function finishDrag(event) {
    const geometry = state.chartGeometry; if (!geometry || state.dragStartX === null) return;
    const start = state.dragStartX; const end = state.dragEndX ?? start; state.dragStartX = null; state.dragEndX = null;
    if (Math.abs(end - start) > 8) { state.viewRange = [geometry.timeAtX(Math.min(start, end)), geometry.timeAtX(Math.max(start, end))]; $("reset-zoom").disabled = false; }
    try { canvas.releasePointerCapture(event.pointerId); } catch (_) {} drawChart();
  }
  canvas.addEventListener("pointerup", finishDrag); canvas.addEventListener("pointercancel", finishDrag);
  canvas.addEventListener("pointerleave", () => { if (state.dragStartX !== null) return; state.hoverTime = null; $("chart-tooltip").hidden = true; drawChart(); });
  canvas.addEventListener("dblclick", resetZoom);
  new ResizeObserver(drawChart).observe($("chart-wrap"));
  load();
})();
