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
    const group = document.createElementNS(svg.namespaceURI, "g");
    links.forEach((link) => {
      const source = byLabel.get(link.source); const detector = byLabel.get(link.detector); if (!source || !detector) return;
      const line = document.createElementNS(svg.namespaceURI, "line");
      line.setAttribute("x1", scaleX(source.x_mm)); line.setAttribute("y1", scaleY(source.y_mm)); line.setAttribute("x2", scaleX(detector.x_mm)); line.setAttribute("y2", scaleY(detector.y_mm));
      line.setAttribute("class", `topology-channel ${link.passed ? "good" : "bad"}`); line.setAttribute("tabindex", "0"); line.dataset.index = link.index;
      line.addEventListener("click", () => { window.location.href = pageURL("/signals.html", { channel: link.index }); });
      line.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); line.click(); } });
      const title = document.createElementNS(svg.namespaceURI, "title"); title.textContent = `${link.label} · ${formatValue(link.distance_mm, 3)} mm · ${link.passed ? "通过" : "需关注"}`; line.appendChild(title); group.appendChild(line);
    });
    svg.appendChild(group);
    points.forEach((point) => {
      const node = document.createElementNS(svg.namespaceURI, "circle"); node.setAttribute("cx", scaleX(point.x_mm)); node.setAttribute("cy", scaleY(point.y_mm)); node.setAttribute("r", point.kind === "source" ? "7" : "5"); node.setAttribute("class", `topology-node ${point.kind}`);
      const title = document.createElementNS(svg.namespaceURI, "title"); title.textContent = `${point.label} (${formatValue(point.x_mm, 2)}, ${formatValue(point.y_mm, 2)}, ${formatValue(point.z_mm, 2)} mm)`; node.appendChild(title); svg.appendChild(node);
    });
    plot.replaceChildren(svg); $("topology-summary").textContent = `${points.length} 个光极 · ${links.length} 条通道 · XY 投影，单位 mm`;
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
