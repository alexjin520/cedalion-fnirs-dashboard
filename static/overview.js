(() => {
  const dashboard = window.Dashboard || {};
  const $ = dashboard.$ || ((id) => document.getElementById(id));
  const number = dashboard.number || new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
  const setConnection = dashboard.setConnection || ((mode, text) => {
    const element = $("connection");
    if (!element) return;
    element.className = `connection ${mode}`;
    if (element.lastElementChild) element.lastElementChild.textContent = text;
  });
  const optionElement = dashboard.optionElement || ((value, label) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  });
  const selectedRecording = () => new URLSearchParams(window.location.search).get("recording");
  const pageURL = dashboard.pageURL || ((path, parameters = {}) => {
    const target = new URL(path, window.location.origin);
    const recording = selectedRecording();
    if (recording) target.searchParams.set("recording", recording);
    Object.entries(parameters).forEach(([name, value]) => {
      if (value === null || value === undefined || value === "") target.searchParams.delete(name);
      else target.searchParams.set(name, value);
    });
    return `${target.pathname}${target.search}${target.hash}`;
  });
  const withRecording = dashboard.withRecording || ((url) => {
    const target = new URL(url, window.location.origin);
    const recording = selectedRecording();
    if (recording && target.pathname.startsWith("/api/") && target.pathname !== "/api/recordings") {
      target.searchParams.set("recording", recording);
    }
    return `${target.pathname}${target.search}${target.hash}`;
  });
  const getJSON = dashboard.getJSON || (async (url) => {
    const response = await fetch(withRecording(url), { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  });
  if (dashboard.initShell) dashboard.initShell("overview");

  // Keep the record picker usable even when an older common.js is cached.
  function recordingLabel(recording) {
    const details = [`${(recording.size_bytes / 1024 / 1024).toFixed(1)} MiB`];
    if (recording.subject) details.push(`sub-${recording.subject}`);
    if (recording.session) details.push(`ses-${recording.session}`);
    return `${recording.filename} · ${details.join(" · ")}`;
  }

  function populateRecordings(inventory, selected) {
    inventory = inventory || { recordings: [] };
    const records = inventory.recordings || [];
    const select = $("recording-select");
    select.replaceChildren(...records.map((recording) => optionElement(recording.id, recordingLabel(recording))));
    const selectedId = selected?.id || inventory.default_recording;
    if (records.some((recording) => recording.id === selectedId)) select.value = selectedId;
    select.disabled = !records.length;
    $("recording-count").textContent = records.length ? `${records.length} 份 SNIRF · 当前分析 ${selected?.analysis_id || "—"}` : "数据目录中没有可选 SNIRF";
    $("manifest-download").href = withRecording("/api/analysis-metadata-export");
    $("report-download").href = withRecording("/api/report-pdf");
  }

  function showAnalysisError(error, inventory) {
    const selectedId = selectedRecording() || inventory?.default_recording;
    const selected = (inventory?.recordings || []).find((recording) => recording.id === selectedId);
    if (inventory) populateRecordings(inventory, { id: selectedId });
    if (selected) {
      $("filename").textContent = selected.filename;
      $("engine-badge").textContent = "Cedalion";
    }
    $("dataset-detail").textContent = error.message;
    $("readiness-title").textContent = "暂时无法读取分析结果";
    $("readiness-detail").textContent = error.message;
    $("readiness-badge").className = "badge badge-bad";
    $("readiness-badge").textContent = "读取失败";
  }

  function renderEvents(items) {
    $("event-chips").replaceChildren(...items.map((item) => {
      const chip = document.createElement("span");
      chip.className = "badge badge-neutral";
      chip.textContent = `${item.label} · ${item.count} 次`;
      return chip;
    }));
  }

  function populate(payload) {
    const summary = payload.summary;
    const quality = payload.quality_summary;
    const motion = payload.motion;
    const task = payload.task;
    const rate = quality.total_channels ? quality.passed_channels / quality.total_channels : 0;
    const healthy = rate >= 0.7;
    $("filename").textContent = summary.filename;
    $("engine-badge").textContent = `Cedalion ${summary.cedalion_version}`;
    const subject = summary.subject?.display_name;
    const subjectPrefix = subject ? `受试者 ${subject} · ` : "";
    $("dataset-detail").textContent = `${subjectPrefix}${(summary.file_size_bytes / 1024 / 1024).toFixed(1)} MiB · ${summary.wavelengths_nm.join(" / ")} nm · DPF ${summary.dpf}`;
    $("quality-pass").textContent = `${quality.passed_channels} / ${quality.total_channels}`;
    $("sample-rate").textContent = summary.sample_rate_hz ? `${number.format(summary.sample_rate_hz)} Hz` : "未知";
    $("samples").textContent = number.format(summary.samples);
    $("duration").textContent = `${number.format(summary.duration_seconds / 60)} min`;
    $("channels").textContent = number.format(summary.channels);
    $("channel-detail").textContent = summary.raw_channels > summary.channels
      ? `${number.format(summary.channels)} / ${number.format(summary.raw_channels)} 个通道纳入分析`
      : `${number.format(summary.channels)} 个分析通道`;
    $("events").textContent = number.format(summary.stimulus_events);
    $("wavelengths").textContent = summary.task_intervals
      ? `${summary.task_intervals} 个任务区间 · ${summary.wavelengths_nm.join(" / ")} nm`
      : `${summary.wavelengths_nm.join(" / ")} nm · ${summary.measurements} 个测量`;
    document.querySelectorAll(".overview-grid .stat-card").forEach((card) => card.classList.remove("loading"));

    $("quality-card-status").className = `badge ${healthy ? "badge-good" : "badge-warning"}`;
    $("quality-card-status").textContent = healthy ? "质量良好" : "需要关注";
    $("quality-card-detail").textContent = `最低 SNR ≥ ${quality.snr_threshold} · 平均 SCI ≥ ${quality.sci_threshold} · PSP 合格窗口 ≥ ${(quality.psp_min_clean_fraction * 100).toFixed(0)}%`;
    $("quality-progress").style.width = `${Math.round(rate * 100)}%`;
    $("quality-progress").style.background = healthy ? "var(--accent)" : "var(--warning)";
    $("quality-card").classList.toggle("warning", !healthy);

    $("readiness-badge").className = `badge ${healthy && task.available ? "badge-good" : "badge-warning"}`;
    $("readiness-badge").textContent = healthy && task.available ? "可以继续" : "建议复核";
    $("quality-percent").textContent = `${Math.round(rate * 100)}%`;
    $("quality-ring").style.setProperty("--score", `${Math.round(rate * 100) * 3.6}deg`);
    $("quality-ring").classList.toggle("warning", !healthy);
    $("readiness-title").textContent = healthy ? "多数通道达到质量门限" : "部分通道未达到质量门限";
    $("readiness-detail").textContent = task.available
      ? `任务分析可用，${task.usable_channels} 个通道已纳入；GVTD 标记 ${motion.flagged_samples} / ${motion.total_samples} 个异常候选采样。`
      : (task.error || "当前记录没有可用的任务分析结果。");
    $("attention-count").textContent = `${quality.total_channels - quality.passed_channels} 个`;
    $("condition-count").textContent = `${task.conditions?.length || 0} 类`;
    $("motion-method").textContent = task.motion_correction || "—";
    renderEvents(task.conditions?.length ? task.conditions : payload.event_counts);
    $("updated-at").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
  }

  async function load() {
    setConnection("waiting", "Cedalion 正在分析");
    let inventory;
    try {
      inventory = await getJSON("/api/recordings");
      populateRecordings(inventory, {
        id: selectedRecording() || inventory.default_recording,
      });
    } catch (error) {
      setConnection("error", "无法读取记录列表");
      $("recording-count").textContent = error.message;
    }

    try {
      const payload = await getJSON("/api/recording");
      populateRecordings(inventory, {
        ...payload.summary.recording,
        analysis_id: payload.summary.analysis?.id,
      });
      populate(payload);
      setConnection("online", `Cedalion ${payload.summary.cedalion_version} 已连接`);
    } catch (error) {
      setConnection("error", "分析服务异常");
      showAnalysisError(error, inventory);
    }
  }

  $("recording-select").addEventListener("change", (event) => {
    window.location.assign(pageURL("/", { recording: event.target.value }));
  });

  load();
})();
