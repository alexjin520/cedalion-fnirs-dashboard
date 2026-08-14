(() => {
  const { $, number, setConnection, formatValue, optionElement, getJSON, pageURL, initShell, renderPageContext } = window.Dashboard;
  initShell("quality");
  const state = { recording: null, quality: [], updating: false, settingsUpdating: false };

  function filteredRows() {
    const query = $("quality-search").value.trim().toLowerCase();
    const attentionOnly = $("attention-only").checked;
    return state.quality.filter((channel) => {
      const matches = !query || [channel.label, channel.source, channel.detector].some((value) => String(value).toLowerCase().includes(query));
      return matches && (!attentionOnly || !channel.passed);
    });
  }

  function renderTable() {
    const labels = ["通道", "光源", "探测器", "平均 / 最低 SNR", "平均 SCI", "PSP 中位数", "PSP 合格窗口", "判定", "人工排除", ""];
    const rows = filteredRows().map((channel) => {
      const row = document.createElement("tr");
      const values = [channel.label, channel.source, channel.detector, `${formatValue(channel.snr, 3)} / ${formatValue(channel.snr_minimum, 3)}`, formatValue(channel.sci, 3), formatValue(channel.psp, 4), channel.psp_clean_fraction == null ? "—" : `${formatValue(channel.psp_clean_fraction * 100, 1)}%`];
      values.forEach((value, index) => { const cell = document.createElement("td"); cell.dataset.label = labels[index]; cell.textContent = value; row.appendChild(cell); });
      const statusCell = document.createElement("td"); statusCell.dataset.label = labels[7];
      const status = document.createElement("span"); status.className = `quality-status ${channel.passed ? "good" : "bad"}`; status.textContent = channel.passed ? "● 通过" : "● 需关注"; statusCell.appendChild(status); row.appendChild(statusCell);
      const manualCell = document.createElement("td"); manualCell.dataset.label = labels[8];
      const manual = document.createElement("input"); manual.type = "checkbox"; manual.checked = Boolean(channel.manual_bad); manual.disabled = state.updating;
      manual.setAttribute("aria-label", `人工排除 ${channel.label}`);
      manual.addEventListener("click", (event) => event.stopPropagation());
      manual.addEventListener("change", () => updateManualDecision(channel.label, manual.checked));
      manualCell.appendChild(manual); row.appendChild(manualCell);
      const actionCell = document.createElement("td"); actionCell.className = "row-action"; actionCell.dataset.label = "操作";
      const link = document.createElement("a"); link.href = pageURL("/signals.html", { channel: channel.index }); link.textContent = "查看信号 →"; actionCell.appendChild(link); row.appendChild(actionCell);
      row.addEventListener("click", (event) => { if (!event.target.closest("a")) window.location.href = link.href; });
      return row;
    });
    if (!rows.length) { const row = document.createElement("tr"); const cell = document.createElement("td"); cell.className = "empty-cell"; cell.colSpan = 10; cell.textContent = "没有符合筛选条件的通道"; row.appendChild(cell); rows.push(row); }
    $("quality-body").replaceChildren(...rows);
  }

  function renderEvents(items) {
    $("event-list").replaceChildren(...items.map((item) => {
      const card = document.createElement("article"); card.className = "event-card";
      const title = document.createElement("strong"); title.textContent = item.label;
      const count = document.createElement("span"); count.textContent = `${item.count} 个事件`;
      const line = document.createElement("i"); card.append(title, count, line); return card;
    }));
  }

  function renderMetadata(summary) {
    const validation = summary.input_validation || {};
    const geometry = validation.geometry || {};
    const distance = geometry.distance_mm || {};
    const parameters = summary.analysis_parameters || {};
    const nuisance = summary.nuisance_regression || {};
    const auxiliary = nuisance.auxiliary || {};
    const global = nuisance.global || {};
    const cbsi = nuisance.cbsi || {};
    const subject = summary.subject || {};
    const compatibility = validation.compatibility || {};
    const warningText = validation.warnings?.length ? `需注意：${validation.warnings.join("；")}` : "通过";
    const compatibilityText = compatibility.temporary_analysis_copy_used
      ? "中文 SubjectID 已在临时分析副本中匿名化；原文件未修改"
      : "未使用临时兼容副本";
    const rows = [
      ["文件", summary.filename], ["文件大小", `${(summary.file_size_bytes / 1024 / 1024).toFixed(2)} MiB`],
      ["受试者", subject.display_name || "未填写"], ["读取兼容性", compatibilityText],
      ["采样点", number.format(summary.samples)], ["采样率", `${number.format(summary.sample_rate_hz)} Hz`],
      ["记录时长", `${number.format(summary.duration_seconds)} s`], ["通道 / 测量", `${summary.channels} / ${summary.measurements}`],
      ["原始 / 分析通道", `${summary.raw_channels} / ${summary.analyzed_channels}`],
      ["光强预处理", `排除 ${summary.excluded_nonpositive_channels} 个无效通道，插值 ${summary.interpolated_samples} 个采样点`],
      ["事件 / 任务区间", `${summary.stimulus_events} / ${summary.task_intervals}`],
      ["波长", `${summary.wavelengths_nm.join(" / ")} nm`], ["Cedalion", summary.cedalion_version],
      ["分析参数", `DPF ${formatValue(parameters.dpf, 3)} · ${parameters.filter_hz?.join("–") || "—"} Hz · SNR ${formatValue(parameters.snr_threshold, 3)}`],
      ["CBSI 血氧校正", `${cbsi.applied ? "已用于任务平均与 GLM" : "未用于任务统计"} · 校正 ${cbsi.corrected_channels ?? "—"} / ${cbsi.channels ?? "—"} 个通道`],
      ["PSP 质量门", `PSP ≥ ${formatValue(parameters.psp_threshold, 4)} · 合格窗口 ≥ ${formatValue((parameters.psp_min_clean_fraction || 0) * 100, 1)}%`],
      ["生理 / 全局回归", `模式 ${parameters.glm?.nuisance_mode || "off"} · 流 ${(parameters.glm?.auxiliary_signal_names || []).join(" / ") || "—"} · 辅助 ${auxiliary.applied ? "已应用" : "未应用"} · 全局 ${global.applied ? `已应用${global.self_included ? "（含自身）" : ""}` : "未应用"}`],
      ["输入校验", warningText],
      ["探头距离", `${formatValue(distance.minimum, 4)}–${formatValue(distance.maximum, 4)} mm · 中位数 ${formatValue(distance.median, 4)}`],
    ];
    $("metadata-list").replaceChildren(...rows.map(([key, value]) => { const row = document.createElement("div"); const dt = document.createElement("dt"); const dd = document.createElement("dd"); dt.textContent = key; dd.textContent = value; row.append(dt, dd); return row; }));
  }

  function populate(recording, quality) {
    const summary = quality.summary;
    const motion = quality.motion;
    const attention = summary.total_channels - summary.passed_channels;
    const rate = summary.total_channels ? summary.passed_channels / summary.total_channels : 0;
    $("quality-pass").textContent = `${summary.passed_channels} / ${summary.total_channels}`;
    $("quality-rate").textContent = `通过率 ${Math.round(rate * 100)}%`;
    $("attention-count").textContent = `${attention} 个`;
    $("snr-threshold").textContent = `≥ ${summary.snr_threshold}`;
    $("sci-threshold").textContent = `≥ ${summary.sci_threshold}`;
    $("sci-window").textContent = `${summary.sci_window_seconds} 秒滑动窗口`;
    $("motion-count").textContent = motion.available ? `${motion.flagged_samples} / ${motion.total_samples}` : "不可用";
    $("motion-rate").textContent = motion.available ? `${(motion.flagged_fraction * 100).toFixed(2)}% · ${motion.segments} 个候选区段` : motion.error;
    $("quality-summary").textContent = `${summary.passed_channels} / ${summary.total_channels} 个通道通过 · 人工排除 ${summary.manual_bad_channels || 0} 个 · 显示 ${filteredRows().length} 个`;
    const gvtd = recording.task?.gvtd || {};
    const pspText = summary.psp_is_quality_gate
      ? `PSP 门限 ${summary.psp_threshold}，合格窗口比例 ≥ ${(summary.psp_min_clean_fraction * 100).toFixed(1)}%`
      : "PSP 仅诊断展示";
    $("manual-qc-note").textContent = `人工排除会写入当前记录的分析清单，并从任务平均和 GLM 中移除。${pspText}。GVTD：${gvtd.reason || "仅标记异常候选"}`;
    renderEvents(recording.event_counts); renderMetadata(recording.summary); renderTable();
  }

  function setFormValue(form, name, value) {
    const field = form.elements.namedItem(name);
    if (field) field.value = value;
  }

  function ensureCbsiControl(form) {
    const fieldset = form.querySelector("fieldset");
    if (!fieldset) return;
    const gvtdField = form.elements.namedItem("gvtd_mode");
    const gvtdExcludeOption = gvtdField?.querySelector('option[value="exclude_epochs"]');
    if (gvtdExcludeOption) gvtdExcludeOption.textContent = "剔除任务试次和 GLM 采样";
    if (!form.elements.namedItem("psp_threshold")) {
      const label = document.createElement("label");
      const title = document.createElement("span"); title.textContent = "PSP 门限";
      const input = document.createElement("input"); input.name = "psp_threshold"; input.type = "number"; input.min = "0"; input.step = "0.01"; input.required = true;
      label.append(title, input); fieldset.append(label);
    }
    if (!form.elements.namedItem("psp_min_clean_fraction")) {
      const label = document.createElement("label");
      const title = document.createElement("span"); title.textContent = "PSP 合格窗口比例";
      const input = document.createElement("input"); input.name = "psp_min_clean_fraction"; input.type = "number"; input.min = "0"; input.max = "1"; input.step = "0.05"; input.required = true;
      label.append(title, input); fieldset.append(label);
    }
    if (!form.elements.namedItem("cbsi_mode")) {
      const label = document.createElement("label");
      const title = document.createElement("span"); title.textContent = "CBSI 血氧校正";
      const select = document.createElement("select"); select.name = "cbsi_mode";
      select.append(optionElement("off", "关闭（仅连续信号比较）"), optionElement("on", "用于任务平均与 GLM"));
      label.append(title, select); fieldset.append(label);
    }
  }

  function populateSettings(payload) {
    const form = $("analysis-settings");
    ensureCbsiControl(form);
    const settings = payload.settings;
    Object.entries(settings).forEach(([name, value]) => {
      if (name !== "glm") setFormValue(form, name, value);
    });
    Object.entries(settings.glm).forEach(([name, value]) => setFormValue(form, `glm_${name}`, value));
    $("settings-status").textContent = `${payload.source} · ${payload.persistence}`;
  }

  function settingsPayload() {
    const form = $("analysis-settings");
    const numberValue = (name) => Number(form.elements.namedItem(name).value);
    return {
      dpf: numberValue("dpf"), filter_min_hz: numberValue("filter_min_hz"), filter_max_hz: numberValue("filter_max_hz"),
      snr_threshold: numberValue("snr_threshold"), sci_threshold: numberValue("sci_threshold"),
      epoch_before_seconds: numberValue("epoch_before_seconds"), epoch_after_seconds: numberValue("epoch_after_seconds"),
      response_start_seconds: numberValue("response_start_seconds"), response_end_seconds: numberValue("response_end_seconds"),
      short_separation_mm: numberValue("short_separation_mm"), short_separation_mode: form.elements.namedItem("short_separation_mode").value,
      gvtd_mode: form.elements.namedItem("gvtd_mode").value,
      psp_threshold: numberValue("psp_threshold"), psp_min_clean_fraction: numberValue("psp_min_clean_fraction"),
      cbsi_mode: form.elements.namedItem("cbsi_mode").value,
      glm: {
        noise_model: form.elements.namedItem("glm_noise_model").value, drift_cutoff_hz: numberValue("glm_drift_cutoff_hz"),
        hrf_sigma_seconds: numberValue("glm_hrf_sigma_seconds"), short_separation_mode: form.elements.namedItem("glm_short_separation_mode").value,
        ar_order: numberValue("glm_ar_order"), nuisance_mode: form.elements.namedItem("glm_nuisance_mode").value,
      },
    };
  }

  async function requestSettings(url, options) {
    const response = await fetch(window.Dashboard.withRecording(url), options);
    const payload = await response.json();
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  async function refreshAnalysis() {
    const [recording, quality] = await Promise.all([getJSON("/api/recording"), getJSON("/api/quality")]);
    state.recording = recording; state.quality = quality.channels; renderPageContext(recording); populate(recording, quality);
  }

  async function updateSettings(reset = false) {
    if (state.settingsUpdating) return;
    state.settingsUpdating = true;
    const submit = $("settings-submit"); const resetButton = $("settings-reset");
    submit.disabled = true; resetButton.disabled = true; $("settings-status").textContent = reset ? "正在恢复服务器默认值并重新分析" : "正在应用设置并重新分析";
    try {
      const payload = await requestSettings(reset ? "/api/settings/reset" : "/api/settings", reset ? { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settingsPayload()) });
      populateSettings(payload);
      await refreshAnalysis();
      $("settings-status").textContent = `${payload.source} · 已重新分析当前记录；${payload.persistence}`;
    } catch (error) {
      $("settings-status").textContent = error.message;
    } finally {
      state.settingsUpdating = false; submit.disabled = false; resetButton.disabled = false;
    }
  }

  async function updateManualDecision(label, checked) {
    if (state.updating) return;
    state.updating = true; renderTable();
    try {
      const labels = state.quality.filter((channel) => channel.manual_bad).map((channel) => channel.label);
      const next = checked ? [...new Set([...labels, label])] : labels.filter((item) => item !== label);
      const response = await fetch(window.Dashboard.withRecording("/api/qc-decisions"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bad_channel_labels: next }),
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
      state.quality = payload.channels;
      const [recording, quality] = await Promise.all([getJSON("/api/recording"), getJSON("/api/quality")]);
      state.recording = recording; state.quality = quality.channels; populate(recording, quality);
    } catch (error) {
      $("quality-summary").textContent = error.message;
    } finally { state.updating = false; renderTable(); }
  }

  async function load() {
    setConnection("waiting", "Cedalion 正在分析");
    try {
      const [recording, quality, settings] = await Promise.all([getJSON("/api/recording"), getJSON("/api/quality"), getJSON("/api/settings")]);
      state.recording = recording; state.quality = quality.channels; renderPageContext(recording); populate(recording, quality);
      populateSettings(settings);
      setConnection("online", `Cedalion ${recording.summary.cedalion_version} 已连接`);
    } catch (error) { setConnection("error", "分析服务异常"); $("quality-summary").textContent = error.message; }
  }

  $("quality-search").addEventListener("input", () => { $("quality-summary").textContent = `显示 ${filteredRows().length} / ${state.quality.length} 个通道`; renderTable(); });
  $("attention-only").addEventListener("change", () => { $("quality-summary").textContent = `显示 ${filteredRows().length} / ${state.quality.length} 个通道`; renderTable(); });
  $("analysis-settings").addEventListener("submit", (event) => { event.preventDefault(); updateSettings(); });
  $("settings-reset").addEventListener("click", () => updateSettings(true));
  document.querySelectorAll(".detail-tabs button").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".detail-tabs button").forEach((item) => { const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-selected", String(active)); });
    document.querySelectorAll(".detail-view").forEach((panel) => panel.classList.toggle("active", panel.id === button.dataset.panel));
  }));
  load();
})();
