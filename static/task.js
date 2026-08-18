(() => {
  const { $, number, setConnection, formatValue, formatSeconds, optionElement, withRecording, getJSON, canvasContext, drawAxes, initShell, renderPageContext } = window.Dashboard;
  initShell("task");
  const state = { recording: null, quality: [], response: null, glm: null };

  function normalizeInferenceState(readiness, available) {
    const stateValue = readiness?.state;
    if (["pending", "unavailable", "exploratory", "ready"].includes(stateValue)) return stateValue;
    if (!available) return "unavailable";
    return readiness?.ready === true ? "ready" : "exploratory";
  }

  function renderInferenceReadiness(readiness, available) {
    const status = normalizeInferenceState(readiness || {}, available);
    const badge = $("glm-readiness-badge");
    const note = $("glm-inference-note");
    if (!badge || !note) return;
    const minimum = Number(readiness?.minimum_trials_per_condition) || 2;
    const insufficient = Array.isArray(readiness?.insufficient_conditions) ? readiness.insufficient_conditions.filter(Boolean) : [];
    const fallbackReason = status === "ready"
      ? `每个条件至少有 ${minimum} 个可用重复试次。`
      : status === "exploratory"
        ? `GLM 数值可以计算，但重复试次不足 ${minimum} 个；当前结果仅供探索，不应作为确认性推断。`
        : status === "pending"
          ? "GLM 尚未计算，首次打开任务统计时会按需执行。"
        : "当前记录没有可用于 GLM 的结果。";
    const baseReason = readiness?.reason || fallbackReason;
    const reason = status === "exploratory" && insufficient.length
      ? `${baseReason.replace(/[。；]+$/, "")}；重复试次不足的条件：${insufficient.join("、")}。`
      : baseReason;
    const labels = { ready: "推断就绪", exploratory: "仅探索性 GLM", pending: "GLM 待计算", unavailable: "GLM 不可用" };
    const classes = { ready: "badge-good", exploratory: "badge-warning", pending: "badge-neutral", unavailable: "badge-bad" };
    badge.className = `badge ${classes[status]}`;
    badge.textContent = labels[status];
    note.replaceChildren();
    const strong = document.createElement("strong");
    strong.textContent = labels[status];
    note.append(strong, document.createTextNode(`：${reason}`));
  }

  function taskChannels() {
    return state.quality.filter((channel) => channel.task_channel_eligible);
  }

  function setupControls() {
    const task = state.recording.task;
    const channels = taskChannels();
    $("task-channels").textContent = `${task.usable_channels} / ${state.recording.summary.channels}`;
    $("task-excluded").textContent = `排除 ${task.excluded_channels} 个未过门限或短距离通道`;
    $("task-method").textContent = `${task.motion_correction} · ${task.filter_hz.join("–")} Hz · 时间窗 ${task.epoch_seconds.join(" 至 ")} 秒${task.warning ? ` · ${task.warning}` : ""}`;
    if (!task.available) { $("task-message").textContent = task.error || "任务分析不可用"; return false; }
    const condition = $("task-condition");
    condition.replaceChildren(...task.conditions.map((item) => optionElement(item.value, `${item.label} · ${item.count} 个区间 · ${number.format(item.duration_seconds)} s`)));
    if (task.conditions.some((item) => item.value === "Tapping/Left")) condition.value = "Tapping/Left";
    condition.disabled = false;
    const channel = $("task-channel");
    const modeledChannels = new Set(task.glm?.channel_labels || []);
    channel.replaceChildren(...channels.map((item) => optionElement(item.index, `${item.label} · ${item.source}–${item.detector}${task.glm?.available && !modeledChannels.has(item.label) ? " · 仅平均" : ""}`)));
    const channelParameter = new URLSearchParams(window.location.search).get("channel");
    const requested = channelParameter === null ? Number.NaN : Number(channelParameter);
    if (channels.some((item) => item.index === requested)) channel.value = requested;
    channel.disabled = !channels.length;
    return Boolean(channels.length);
  }

  function setupGlmControls() {
    const glm = state.recording.task.glm || {};
    const contrast = $("glm-contrast");
    const pending = glm.status === "pending" || glm.pending === true || state.recording.task.inference_readiness?.pending === true;
    if (!glm.available) {
      renderInferenceReadiness(glm.inference_readiness || state.recording.task.inference_readiness, false);
      if (pending) {
        contrast.replaceChildren(optionElement("", "GLM 待计算"));
        contrast.disabled = true;
        $("glm-export").disabled = true;
        $("glm-method").textContent = "首次请求 GLM 统计时按需拟合";
        $("glm-message").textContent = "GLM 尚未计算；任务平均可以先行查看。";
        return false;
      }
      contrast.replaceChildren(optionElement("", "GLM 不可用"));
      contrast.disabled = true;
      $("glm-export").disabled = true;
      $("glm-method").textContent = glm.error || "当前记录没有可用的 GLM 统计结果";
      $("glm-message").textContent = glm.error || "GLM 统计不可用；描述性任务平均仍可正常查看。";
      return false;
    }
    renderInferenceReadiness(glm.inference_readiness || state.recording.task.inference_readiness, true);
    const previous = contrast.value;
    contrast.replaceChildren(...glm.contrasts.map((item) => optionElement(item.value, item.label)));
    if (glm.contrasts.some((item) => item.value === previous)) contrast.value = previous;
    contrast.disabled = !glm.contrasts.length;
    $("glm-export").disabled = false;
    const model = glm.model;
    const short = glm.short_separation;
    const auxiliary = glm.auxiliary || {};
    const global = glm.global || {};
    const gvtd = glm.gvtd || {};
    const filter = model.filter_hz ? `${model.filter_hz.join("–")} Hz` : "AR-IRLS 未预滤波";
    const nuisance = [short.applied && "短间距", auxiliary.applied && "辅助", global.applied && "全局"].filter(Boolean);
    const censoring = gvtd.applied && gvtd.excluded_samples ? ` · GVTD 删点 ${gvtd.excluded_samples}` : "";
    $("glm-method").textContent = `${model.noise_model.toUpperCase()} · Gamma σ ${number.format(model.hrf_sigma_seconds)} s · 漂移 ${model.drift_cutoff_hz} Hz · ${filter}${nuisance.length ? ` · ${nuisance.join("+")}回归` : ""}${censoring}`;

    const currentIndex = Number($("task-channel").value);
    const current = state.quality.find((channel) => channel.index === currentIndex);
    if (current && !glm.channel_labels.includes(current.label)) {
      const replacement = state.quality.find((channel) => glm.channel_labels.includes(channel.label));
      if (replacement) $("task-channel").value = replacement.index;
    }
    return true;
  }

  async function loadResponse() {
    if (!state.recording?.task?.available || $("task-channel").disabled) return;
    $("task-message").classList.remove("hidden"); $("task-message").textContent = "正在获取分段平均结果…";
    const query = new URLSearchParams({ condition: $("task-condition").value, channel: $("task-channel").value, max_points: "600" });
    try {
      const payload = await getJSON(`/api/task-response?${query}`);
      state.response = payload;
      const single = payload.condition.count < 2;
      $("task-title").textContent = `${payload.condition.label} · ${single ? "单次" : "平均"}任务血氧响应`;
      $("task-subtitle").textContent = `${payload.channel.label}（${payload.channel.source}–${payload.channel.detector}）· ${single ? "单个区间，不提供 SEM" : "平均值 ± SEM"}`;
      $("task-epochs").textContent = `${payload.condition.count} 个`;
      $("task-epochs-detail").textContent = single ? "仅供查看，不能进行重复试次统计" : `基线 ${state.recording.task.baseline_seconds.join(" 至 ")} 秒`;
      $("hbo-peak").textContent = `${formatValue(payload.metrics.hbo_peak.amplitude)} µM`;
      $("hbo-latency").textContent = `峰值潜伏期 ${formatSeconds(payload.metrics.hbo_peak.latency_seconds)}`;
      $("hbr-trough").textContent = `${formatValue(payload.metrics.hbr_trough.amplitude)} µM`;
      $("hbr-latency").textContent = `谷值潜伏期 ${formatSeconds(payload.metrics.hbr_trough.latency_seconds)}`;
      $("task-message").classList.add("hidden"); $("export").disabled = false; $("task-png").disabled = false;
      drawChart();
    } catch (error) { $("task-message").textContent = error.message; }
  }

  function formatPValue(value) {
    const numberValue = Number(value);
    if (!Number.isFinite(numberValue)) return "—";
    return numberValue < 0.001 ? numberValue.toExponential(2) : numberValue.toFixed(4);
  }

  function formatConfidenceInterval(interval) {
    if (!Array.isArray(interval)) return "—";
    return `[${formatValue(interval[0], 4)}, ${formatValue(interval[1], 4)}]`;
  }

  function renderGlmRows(bodyId, rows, estimateKey, emptyText) {
    const labels = ["成分", estimateKey === "beta" ? "Beta" : "效应差", "95% CI", "t", "p", "q (FDR)", "R²"];
    const tableRows = rows.map((row) => {
      const element = document.createElement("tr");
      const values = [row.chromo, formatValue(row[estimateKey], 4), formatConfidenceInterval(row.confidence_interval_95), formatValue(row.t_value, 3), formatPValue(row.p_value), formatPValue(row.q_value), formatValue(row.r_squared, 3)];
      values.forEach((value, index) => { const cell = document.createElement("td"); cell.dataset.label = labels[index]; cell.textContent = value; element.appendChild(cell); });
      return element;
    });
    if (!tableRows.length) {
      const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = labels.length; cell.className = "empty-cell"; cell.textContent = emptyText; row.appendChild(cell); tableRows.push(row);
    }
    $(bodyId).replaceChildren(...tableRows);
  }

  async function loadGlm() {
    if (!state.recording?.task?.available || $("task-channel").disabled) return;
    const pending = state.recording.task.glm?.status === "pending" || state.recording.task.inference_readiness?.pending === true;
    const query = new URLSearchParams({ condition: $("task-condition").value });
    query.set("channel", pending ? "auto" : $("task-channel").value);
    if (!$("glm-contrast").disabled) query.set("contrast", $("glm-contrast").value);
    try {
      const payload = await getJSON(`/api/task-glm?${query}`);
      state.glm = payload;
      state.recording.task.glm = payload.summary;
      state.recording.task.inference_readiness = payload.inference_readiness || payload.summary?.inference_readiness || state.recording.task.inference_readiness;
      setupGlmControls();
      const previousChannel = String($("task-channel").value);
      if (payload.channel?.index !== undefined) $("task-channel").value = String(payload.channel.index);
      if (previousChannel !== String($("task-channel").value)) await loadResponse();
      renderInferenceReadiness(payload.inference_readiness || payload.summary?.inference_readiness || state.recording.task.glm?.inference_readiness || state.recording.task.inference_readiness, true);
      $("glm-subtitle").textContent = `${payload.channel.label}（${payload.channel.source}–${payload.channel.detector}）· ${payload.condition.label} 的 Gamma HRF 系数`;
      $("glm-condition-title").textContent = `${payload.condition.label} 条件效应`;
      renderGlmRows("glm-condition-body", payload.condition_effects, "beta", "没有该条件的 GLM 统计量");
      if (payload.contrast) {
        $("glm-contrast-title").textContent = `${payload.contrast.label} 对比`;
        renderGlmRows("glm-contrast-body", payload.contrast_effects, "effect", "没有该对比的 GLM 统计量");
      } else {
        $("glm-contrast-title").textContent = "条件对比";
        renderGlmRows("glm-contrast-body", [], "effect", "至少需要两个任务条件");
      }
      const short = payload.summary.short_separation;
      const auxiliary = payload.summary.auxiliary || {};
      const global = payload.summary.global || {};
      const gvtd = payload.summary.gvtd || {};
      const auxiliaryText = auxiliary.applied
        ? `辅助回归：${auxiliary.used_regressors.map((item) => item.name).join("、")}。`
        : `辅助回归：${auxiliary.reason || "未应用"}。`;
      const globalText = global.applied
        ? `全局平均回归已应用${global.self_included ? "（包含当前建模通道）" : ""}。`
        : `全局回归：${global.reason || "未应用"}。`;
      const gvtdText = gvtd.applied
        ? `GVTD：剔除 ${gvtd.excluded_samples ?? 0} / ${gvtd.total_samples ?? "—"} 个 GLM 采样。`
        : `GVTD：${gvtd.reason || "仅标记异常候选"}。`;
      const fallbackText = payload.summary.model.noise_model_fallback ? `${payload.summary.model.noise_model_fallback}。` : "";
      $("glm-message").textContent = `${payload.summary.model.input}；${short.applied ? `${short.reason}。` : `${short.reason}，未加入短间距回归。`} ${auxiliaryText} ${globalText} ${gvtdText} ${fallbackText}q 值按条件/对比及血红蛋白成分跨建模通道进行 Benjamini-Hochberg 校正。`;
    } catch (error) {
      $("glm-message").textContent = error.message;
      renderGlmRows("glm-condition-body", [], "beta", error.message);
      renderGlmRows("glm-contrast-body", [], "effect", error.message);
    }
  }

  function drawChart() {
    if (!state.response?.series?.length) return;
    const surface = canvasContext("task-chart"); if (!surface) return;
    const allPoints = state.response.series.flatMap((series) => series.points).filter((point) => point[1] !== null);
    if (!allPoints.length) return;
    const xs = allPoints.map((point) => point[0]);
    const ys = allPoints.flatMap((point) => { const sem = Number.isFinite(point[2]) ? point[2] : 0; return [point[1] - sem, point[1] + sem]; });
    let ymin = Math.min(...ys, 0); let ymax = Math.max(...ys, 0);
    const margin = Math.max((ymax - ymin) * 0.1, 0.001); ymin -= margin; ymax += margin;
    const axes = drawAxes(surface.ctx, surface.width, surface.height, { xmin: Math.min(...xs), xmax: Math.max(...xs), ymin, ymax }, (value) => `${number.format(value)}s`);
    const stimulus = state.response.stimulus;
    surface.ctx.fillStyle = axes.theme.stimulusFill;
    surface.ctx.fillRect(axes.x(stimulus.onset_seconds), axes.pad.top, axes.x(stimulus.onset_seconds + stimulus.duration_seconds) - axes.x(stimulus.onset_seconds), axes.plotHeight);
    surface.ctx.save(); surface.ctx.setLineDash([4, 4]); surface.ctx.strokeStyle = axes.theme.axis;
    surface.ctx.beginPath(); surface.ctx.moveTo(axes.pad.left, axes.y(0)); surface.ctx.lineTo(surface.width - axes.pad.right, axes.y(0)); surface.ctx.stroke(); surface.ctx.restore();
    surface.ctx.fillStyle = axes.theme.text; surface.ctx.fillText("µM", 9, 16);
    const colors = { HbO: axes.theme.hbo, HbR: axes.theme.hbr };
    state.response.series.forEach((series) => {
      const points = series.points.filter((point) => point[1] !== null);
      surface.ctx.fillStyle = `${colors[series.name]}24`; surface.ctx.beginPath();
      points.forEach((point, index) => { const sem = Number.isFinite(point[2]) ? point[2] : 0; if (!index) surface.ctx.moveTo(axes.x(point[0]), axes.y(point[1] + sem)); else surface.ctx.lineTo(axes.x(point[0]), axes.y(point[1] + sem)); });
      [...points].reverse().forEach((point) => { const sem = Number.isFinite(point[2]) ? point[2] : 0; surface.ctx.lineTo(axes.x(point[0]), axes.y(point[1] - sem)); });
      surface.ctx.closePath(); surface.ctx.fill(); surface.ctx.strokeStyle = colors[series.name]; surface.ctx.lineWidth = 2; surface.ctx.beginPath();
      points.forEach((point, index) => { if (!index) surface.ctx.moveTo(axes.x(point[0]), axes.y(point[1])); else surface.ctx.lineTo(axes.x(point[0]), axes.y(point[1])); }); surface.ctx.stroke();
    });
  }

  function taskQuery() { return new URLSearchParams({ condition: $("task-condition").value, channel: $("task-channel").value }); }
  function exportCSV() { if (state.response) { const link = document.createElement("a"); link.href = withRecording(`/api/task-export?${taskQuery()}`); link.click(); } }
  function exportGlmCSV() { if (state.recording?.task?.available) { const link = document.createElement("a"); link.href = withRecording("/api/task-glm-export"); link.click(); } }
  function exportPNG() {
    if (!state.response) return;
    $("task-chart").toBlob((blob) => {
      if (!blob) return;
      const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `task-${state.response.condition.value.replace("/", "-")}-${state.response.channel.label}.png`; link.click(); URL.revokeObjectURL(link.href);
    }, "image/png");
  }

  async function reloadSelectedAnalysis() {
    await Promise.all([loadResponse(), loadGlm()]);
  }

  async function load() {
    setConnection("waiting", "Cedalion 正在分析");
    try {
      const [recording, quality] = await Promise.all([getJSON("/api/recording"), getJSON("/api/quality")]);
      state.recording = recording; state.quality = quality.channels; renderPageContext(recording);
      setupControls(); setupGlmControls(); setConnection("online", `Cedalion ${recording.summary.cedalion_version} 已连接`); await reloadSelectedAnalysis();
    } catch (error) { setConnection("error", "分析服务异常"); $("task-message").textContent = error.message; $("glm-message").textContent = error.message; }
  }

  $("task-condition").addEventListener("change", reloadSelectedAnalysis); $("task-channel").addEventListener("change", reloadSelectedAnalysis); $("glm-contrast").addEventListener("change", loadGlm);
  $("export").addEventListener("click", exportCSV); $("glm-export").addEventListener("click", exportGlmCSV); $("task-png").addEventListener("click", exportPNG);
  new ResizeObserver(drawChart).observe($("task-chart-wrap"));
  load();
})();
