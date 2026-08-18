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
  let inventoryState = null;

  // Keep the record picker usable even when an older common.js is cached.
  function recordingLabel(recording) {
    const details = [`${(recording.size_bytes / 1024 / 1024).toFixed(1)} MiB`];
    if (recording.subject) details.push(`sub-${recording.subject}`);
    if (recording.session) details.push(`ses-${recording.session}`);
    if (recording.is_uploaded) details.push("已上传");
    return `${recording.filename} · ${details.join(" · ")}`;
  }

  function setActionStatus(message = "", mode = "") {
    const status = $("recording-action-status");
    status.textContent = message;
    status.className = `recording-action-status ${mode}`.trim();
  }

  function selectedInventoryRecord() {
    const selectedId = selectedRecording() || inventoryState?.default_recording;
    return (inventoryState?.recordings || []).find((recording) => recording.id === selectedId) || null;
  }

  function updateRecordingActions(recording) {
    const deleteButton = $("delete-recording");
    const deletable = Boolean(recording?.is_uploaded);
    deleteButton.disabled = !deletable;
    deleteButton.title = deletable ? "删除已上传文件" : "内置样例不能删除";
    deleteButton.setAttribute("aria-label", deleteButton.title);
  }

  function populateRecordings(inventory, selected) {
    inventory = inventory || { recordings: [] };
    inventoryState = inventory;
    const records = inventory.recordings || [];
    const select = $("recording-select");
    select.replaceChildren(...records.map((recording) => optionElement(recording.id, recordingLabel(recording))));
    const selectedId = selected?.id || inventory.default_recording;
    if (records.some((recording) => recording.id === selectedId)) select.value = selectedId;
    const selectedRecord = records.find((recording) => recording.id === select.value) || null;
    select.disabled = !records.length;
    const uploadedCount = records.filter((recording) => recording.is_uploaded).length;
    $("recording-count").textContent = records.length
      ? `${records.length} 份 SNIRF · 已上传 ${uploadedCount} 份 · 当前分析 ${selected?.analysis_id || "—"}`
      : "数据目录中没有可选 SNIRF";
    updateRecordingActions(selectedRecord);
    $("manifest-download").href = withRecording("/api/analysis-metadata-export");
    $("report-download").href = withRecording("/api/report-pdf");
  }

  async function responseJSON(response) {
    let payload;
    try {
      payload = await response.json();
    } catch (_) {
      throw new Error(`服务器返回异常（HTTP ${response.status}）`);
    }
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  async function uploadRecording(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".snirf")) {
      setActionStatus("请选择 .snirf 文件", "error");
      return;
    }
    const button = $("upload-recording");
    const input = $("recording-upload");
    button.disabled = true;
    button.classList.add("busy");
    input.disabled = true;
    setActionStatus(`正在上传 ${file.name}…`);
    try {
      const payload = await responseJSON(await fetch("/api/uploads", {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-FNIRS-Filename": encodeURIComponent(file.name),
        },
        body: file,
      }));
      setActionStatus("上传完成，正在切换记录…", "success");
      window.location.assign(pageURL("/", { recording: payload.recording.id }));
    } catch (error) {
      setActionStatus(error.message, "error");
    } finally {
      button.disabled = false;
      button.classList.remove("busy");
      input.disabled = false;
      input.value = "";
    }
  }

  async function deleteRecording() {
    const recording = selectedInventoryRecord();
    if (!recording?.is_uploaded) return;
    if (!window.confirm(`删除已上传文件“${recording.filename}”？此操作无法恢复。`)) return;
    const button = $("delete-recording");
    button.disabled = true;
    setActionStatus(`正在删除 ${recording.filename}…`);
    try {
      await responseJSON(await fetch(`/api/uploads?recording=${encodeURIComponent(recording.id)}`, {
        method: "DELETE",
      }));
      setActionStatus("已删除，正在切换到默认记录…", "success");
      window.location.assign(pageURL("/", { recording: inventoryState.default_recording }));
    } catch (error) {
      setActionStatus(error.message, "error");
      updateRecordingActions(recording);
    }
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
    const inference = task?.inference_readiness || task?.glm?.inference_readiness || null;
    const inferenceState = ["pending", "unavailable", "exploratory", "ready"].includes(inference?.state)
      ? inference.state
      : (inference ? (inference.ready === true ? "ready" : "exploratory") : null);
    const taskReady = task.available && inferenceState !== "pending" && (!inferenceState || inferenceState === "ready");
    const readinessBadgeText = healthy && inferenceState === "pending"
      ? "GLM 待计算"
      : healthy && taskReady
      ? "可以继续"
      : (healthy && inferenceState === "exploratory" ? "探索性结果" : "建议复核");
    let readinessTitle = "多数通道达到质量门限";
    if (!healthy) readinessTitle = "部分通道未达到质量门限";
    else if (!task.available) readinessTitle = "任务分析不可用";
    else if (inferenceState === "pending") readinessTitle = "GLM 待计算，任务平均可先查看";
    else if (inferenceState === "exploratory") readinessTitle = "GLM 可计算，但当前仅供探索";
    else if (inferenceState === "unavailable") readinessTitle = "任务可用，但 GLM 推断不可用";
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

    $("readiness-badge").className = `badge ${healthy && inferenceState === "pending" ? "badge-neutral" : healthy && taskReady ? "badge-good" : "badge-warning"}`;
    $("readiness-badge").textContent = readinessBadgeText;
    $("quality-percent").textContent = `${Math.round(rate * 100)}%`;
    $("quality-ring").style.setProperty("--score", `${Math.round(rate * 100) * 3.6}deg`);
    $("quality-ring").classList.toggle("warning", !healthy);
    $("readiness-title").textContent = readinessTitle;
    $("readiness-detail").textContent = task.available
      ? `任务分析可用，${task.usable_channels} 个通道已纳入；${inferenceState && inferenceState !== "ready" ? (inference?.reason || "当前不具备重复试次推断就绪度。") + " " : ""}GVTD 标记 ${motion.flagged_samples} / ${motion.total_samples} 个异常候选采样。`
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
  $("upload-recording").addEventListener("click", () => $("recording-upload").click());
  $("recording-upload").addEventListener("change", (event) => uploadRecording(event.target.files[0]));
  $("delete-recording").addEventListener("click", deleteRecording);

  load();
})();
