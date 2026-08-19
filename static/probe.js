(() => {
  const { $, setConnection, formatValue, getJSON, pageURL, initShell, renderPageContext } = window.Dashboard;
  initShell("probe");

  function renderTopology(probe) {
    const plot = $("topology-plot");
    const points = probe?.geometry?.optode_positions_mm || [];
    const links = probe?.channels || [];
    if (!points.length) {
      $("topology-summary").textContent = "该记录没有可用的三维坐标";
      plot.replaceChildren(Object.assign(document.createElement("p"), { className: "empty-state", textContent: "SNIRF 未提供可用探头坐标" }));
      return;
    }

    const byLabel = new Map(points.map((point) => [point.label, point]));
    const xs = points.map((point) => point.x_mm); const ys = points.map((point) => point.y_mm);
    const minX = Math.min(...xs); const maxX = Math.max(...xs); const minY = Math.min(...ys); const maxY = Math.max(...ys);
    const pad = 42; const width = 900; const height = 500;
    const scaleX = (value) => pad + ((value - minX) / Math.max(maxX - minX, 1e-9)) * (width - pad * 2);
    const scaleY = (value) => height - pad - ((value - minY) / Math.max(maxY - minY, 1e-9)) * (height - pad * 2);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "探头拓扑图");
    const lineGroup = document.createElementNS(svg.namespaceURI, "g");
    const nodeGroup = document.createElementNS(svg.namespaceURI, "g");
    const lineElements = []; const nodeElements = [];
    let focusLabel = ""; let hoverLabel = "";

    const focusLabelForView = () => hoverLabel || focusLabel;
    const updateView = () => {
      const mode = $("topology-mode").value;
      const active = focusLabelForView();
      lineElements.forEach(({ element, link }) => {
        const adjacent = Boolean(active && (link.source === active || link.detector === active));
        const baseVisible = mode === "all" || (mode === "passed" && link.passed) || (mode === "neighbors" && adjacent);
        const focused = adjacent && mode !== "hidden";
        element.classList.toggle("is-hidden", !baseVisible || mode === "hidden");
        element.classList.toggle("is-focus", focused);
        element.classList.toggle("is-muted", Boolean(active && baseVisible && !focused));
      });
      nodeElements.forEach(({ element, label }) => {
        const activeNode = label === active;
        const adjacentNode = Boolean(active && links.some((link) => (link.source === active && link.detector === label) || (link.detector === active && link.source === label)));
        element.classList.toggle("is-focus", activeNode);
        element.classList.toggle("is-adjacent", adjacentNode);
      });
      const visibleCount = lineElements.filter(({ element }) => !element.classList.contains("is-hidden")).length;
      $("topology-note").textContent = mode === "neighbors"
        ? (active ? `${active} · 显示 ${visibleCount} 条相邻通道；点击通道进入信号浏览` : "选择或悬停光极查看相邻通道；点击通道进入信号浏览")
        : `${visibleCount} / ${links.length} 条通道可见；可选择光极突出相邻通道`;
    };

    links.forEach((link) => {
      const source = byLabel.get(link.source); const detector = byLabel.get(link.detector); if (!source || !detector) return;
      const line = document.createElementNS(svg.namespaceURI, "line");
      line.setAttribute("x1", scaleX(source.x_mm)); line.setAttribute("y1", scaleY(source.y_mm)); line.setAttribute("x2", scaleX(detector.x_mm)); line.setAttribute("y2", scaleY(detector.y_mm));
      line.setAttribute("class", `topology-channel ${link.passed ? "good" : "bad"}`); line.setAttribute("tabindex", "0");
      line.addEventListener("click", () => { window.location.href = pageURL("/signals.html", { channel: link.index }); });
      line.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); line.click(); } });
      const title = document.createElementNS(svg.namespaceURI, "title"); title.textContent = `${link.label} · ${formatValue(link.distance_mm, 3)} mm · ${link.passed ? "通过" : "需关注"}`; line.appendChild(title);
      lineGroup.appendChild(line); lineElements.push({ element: line, link });
    });
    svg.appendChild(lineGroup);

    points.forEach((point) => {
      const node = document.createElementNS(svg.namespaceURI, "circle");
      node.setAttribute("cx", scaleX(point.x_mm)); node.setAttribute("cy", scaleY(point.y_mm)); node.setAttribute("r", point.kind === "source" ? "7" : "5"); node.setAttribute("class", `topology-node ${point.kind}`); node.setAttribute("tabindex", "0");
      node.addEventListener("mouseenter", () => { hoverLabel = point.label; updateView(); });
      node.addEventListener("mouseleave", () => { hoverLabel = ""; updateView(); });
      node.addEventListener("click", () => { focusLabel = focusLabel === point.label ? "" : point.label; $("topology-focus").value = focusLabel; updateView(); });
      node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); node.click(); } });
      const title = document.createElementNS(svg.namespaceURI, "title"); title.textContent = `${point.label} (${formatValue(point.x_mm, 2)}, ${formatValue(point.y_mm, 2)}, ${formatValue(point.z_mm, 2)} mm)`; node.appendChild(title);
      nodeGroup.appendChild(node); nodeElements.push({ element: node, label: point.label });
    });
    svg.appendChild(nodeGroup); plot.replaceChildren(svg);
    $("topology-summary").textContent = `${points.length} 个光极 · ${links.length} 条通道 · XY 投影，单位 mm`;

    const focus = $("topology-focus");
    focus.replaceChildren(Object.assign(document.createElement("option"), { value: "", textContent: "选择光源或探测器" }));
    points.slice().sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true })).forEach((point) => focus.appendChild(Object.assign(document.createElement("option"), { value: point.label, textContent: `${point.label} · ${point.kind === "source" ? "光源" : "探测器"}` })));
    $("topology-mode").onchange = updateView;
    focus.onchange = () => { focusLabel = focus.value; updateView(); };
    $("topology-reset").onclick = () => { focusLabel = ""; hoverLabel = ""; focus.value = ""; $("topology-mode").value = "neighbors"; updateView(); };
    updateView();
  }

  async function load() {
    setConnection("waiting", "Cedalion 正在读取探头");
    try {
      const [recording, probe] = await Promise.all([getJSON("/api/recording"), getJSON("/api/probe")]);
      renderPageContext(recording); renderTopology(probe);
      setConnection("online", `Cedalion ${recording.summary.cedalion_version} 已连接`);
    } catch (error) {
      setConnection("error", "分析服务异常"); $("topology-summary").textContent = error.message;
    }
  }

  load();
})();
