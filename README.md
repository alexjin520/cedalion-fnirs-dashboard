# Cedalion fNIRS Analysis Dashboard

服务端使用 Cedalion 读取和分析 SNIRF，浏览器只接收摘要、质量指标、降采样连续曲线、任务平均和 GLM 统计结果。当前功能及界面位置见 `FEATURES.md`。

当前分析流程：

```text
连续信号：SNIRF → 时间轴校验 / 受控重采样 → 有效光强通道 → OD → HbO/HbR → 可选 TDDR / CBSI / Wavelet 比较 → 0.01–0.5 Hz 滤波
质量评估：原始光强 → SNR / SCI / PSP + GVTD → 自动与人工通道筛选、运动候选区段
任务平均：事件区间 → OD → TDDR → HbO/HbR → 可选 CBSI → 滤波 → 分段 → 基线校正 → 条件响应
任务 GLM：事件区间 → TDDR → HbO/HbR → 可选 CBSI → Gamma HRF + 余弦漂移 (+ 可选短间距、辅助生理或全局回归) → beta / 95% CI / t / p / FDR q；模型可计算不等同于具备重复试次推断就绪度
```

## 数据文件

默认读取当前设备记录的分析副本：

```text
data/samples/recording_20260419_173115.snirf
```

原始文件的中文 `SubjectID` 与 pysnirf2 的 ASCII 限制冲突，因此该副本只将
`SubjectID` 匿名化为 `subject-001`；信号、探头位置和刺激标记均未修改。官方
手指敲击样例和原来的短测试文件仍保存在：

```text
data/samples/fingertapping.snirf
data/samples/mne_nirsport2_raw.snirf
```

仪表盘也支持原始 SNIRF 中以 UTF-8 保存的中文 `SubjectID`。服务端直接读取该字段
供总览与分析清单显示；若 Cedalion 的 pysnirf2 读取器不能处理其中的非 ASCII 字符，
服务端会创建仅在当前读取期间存在的临时副本，并只将副本的 `SubjectID` 改为稳定的
ASCII 伪名。该副本仍使用 SNIRF 规范要求的 HDF5 可变长度字符串格式。项目以
[SNIRF file specification](https://github.com/fNIRS/snirf/blob/master/snirf_specification.md)
作为格式校验基准；原始 SNIRF、信号、探头位置和刺激标记均不会被修改。请只在受控
访问的部署中显示可识别受试者信息。

`FNIRS_DATA_DIR` 下的所有 `.snirf` 文件会出现在总览页“分析记录”选择器中。默认配置使用
`data/samples` 保存内置样例，并使用 `data/username` 保存网页上传的文件；上传文件会和
内置样例一起出现在选择器中。选择后
浏览器会以 `?recording=<相对路径>` 保持该记录，并将相同选择传递给信号、任务、质量
页面和导出接口；服务端按文件路径、大小和修改时间保留最多 3 份内存分析缓存，不会在
列出目录时预先分析全部文件。只接受位于 `FNIRS_DATA_DIR` 内的相对 `.snirf` 路径。

总览页的“上传 SNIRF”会把文件以流式方式写入 `FNIRS_UPLOAD_DIR`（默认是
`FNIRS_DATA_DIR/username`），只接受 HDF5/SNIRF 文件，默认大小上限为 1 GiB，可用
`FNIRS_MAX_UPLOAD_BYTES` 调整。选择已上传记录后，旁边的删除按钮可以删除该上传文件；
内置样例和上传目录外的文件不能通过此接口删除。当前服务没有登录鉴权，因此上传目录是
所有能访问 dashboard 的用户共享的。

不要把 SNIRF 放进 `static/`，该目录的内容会直接暴露给浏览器。

## 当前机器启动

本工程已建立本地隔离环境，可直接启动：

```bash
.conda-env/bin/python server.py --host 0.0.0.0 --port 10000
```

然后打开 `http://localhost:10000`。

## 新服务器安装

Cedalion 26.5.1 要求 Python 3.11 或更高版本：

```bash
sudo apt-get install fonts-noto-cjk
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-cedalion.txt
git clone --depth 1 --branch v26.5.1 https://github.com/ibs-lab/cedalion.git .vendor/cedalion
.venv/bin/pip install --no-deps .vendor/cedalion
.venv/bin/python server.py --host 0.0.0.0 --port 10000
```

生产数据可放在独立目录：

```bash
FNIRS_DATA_DIR=/opt/fnirs-dashboard/data \
FNIRS_DEFAULT_FILE=recording.snirf \
.venv/bin/python server.py --host 0.0.0.0 --port 10000
```

浏览器访问 `http://服务器IP:10000`，健康检查为 `/api/health`；健康检查只确认所选（或默认）文件存在，不触发完整分析。

没有 systemd 权限的账号可使用项目内脚本后台运行：

```bash
bash start-server.sh
bash status-server.sh
bash stop-server.sh
tail -f .runtime/server.log
```

后台进程会在退出 SSH 后继续运行，但服务器重启后需要重新执行 `bash start-server.sh`。

如果 SSH 环境的 PID 1 是带 `--die-with-parent` 的 `bwrap`，退出 SSH 会强制销毁所有后台进程，`start-server.sh` 会拒绝给出错误的“已常驻”提示。此时只能保持 SSH 窗口并前台运行：

```bash
cd /srv/testdata/projects/fnirs-dashboard
bash run-server.sh
```

真正无人值守运行需要管理员在沙箱外配置 systemd、容器或反向代理服务。

## 回归测试与 CI

本地完整回归使用：

```bash
.conda-env/bin/python -m py_compile server.py test_*.py
.conda-env/bin/python -m unittest discover -v
```

仓库中的 `.github/workflows/fnirs-dashboard.yml` 会在 Python 3.11 环境安装固定的
Cedalion 26.5.1，执行编译、单元测试、进程内 API/导出契约测试，并下载带 SHA-256
校验的官方 `mne_nirsport2_raw.snirf` 做真实分析 smoke test。三份设备样例的完整摘要
回归使用 `test_real_snirf.py`；在本地或测试服务器上可显式指定：

```bash
FNIRS_REAL_SAMPLE_DIR=/path/to/data/samples \
.conda-env/bin/python -m unittest -v test_real_snirf
```

指定目录时，`fingertapping.snirf`、`mne_nirsport2_raw.snirf` 和
`recording_20260419_173115.snirf` 任一缺失都会使测试失败；分析前后还会比较源文件
SHA-256，确保真实 SNIRF 不被修改。

## 页面和接口

工作台按分析任务拆成四个页面，共享顶部导航并适配窄屏：

- `/`：记录总览、中文 `SubjectID` 显示、SNIRF 选择、质量就绪度、分析清单下载和分析入口
- `/signals`（兼容 `/signals.html`）：连续光强、OD、HbO/HbR、任务区间与 GVTD 异常候选；滤波信号可比较未校正、TDDR、TDDR + CBSI 和实验性 Wavelet，支持 `?channel=3` 直达指定通道
- `/task`（兼容 `/task.html`）：任务锁定平均响应，以及逐通道的条件 GLM beta、95% CI、t/p/q、条件对比和实际辅助/全局回归状态；GLM 即使计算可用，也会单独标注重复试次推断就绪度
- `/quality`（兼容 `/quality.html`）：逐通道 SNR/SCI/PSP、GVTD 摘要、人工坏通道标记、事件标记、文件元信息、辅助/全局回归状态与分析流程

总览页的“生成 PDF 报告”会为当前记录生成单条分析报告，包含元信息、质量事实、代表性任务响应图、GLM 状态、完整分析参数和研究者备注位置。报告不会自动替研究者解释实验假设；任务或 GLM 不可用时会记录明确原因。

PDF 使用同时覆盖中文、英文、数字和科学单位的字体，避免标签可见而数据值丢失。Docker 镜像已安装 `fonts-noto-cjk`；独立部署请安装同一字体包，或通过 `FNIRS_REPORT_FONT=/path/to/font.ttf` 指定相应字体文件。

任务曲线显示 HbO/HbR 平均值与 SEM；响应峰值在刺激后 3–15 秒内提取。页面支持导出当前条件/通道的 CSV 和 PNG。任务与 GLM CSV 均包含 `analysis_id`、输入 SHA-256 和记录 ID。GLM 区域使用与上方相同的条件和通道，显示 Gamma HRF 系数、95% CI、t/p、跨建模通道的 Benjamini-Hochberg FDR q 值和可选条件对比；“导出 GLM CSV”包含所有建模通道的条件和对比结果，并带有 `inference_state`、`inference_ready` 和 `inference_reason`。GLM 的数值“可用”只表示模型计算成功；任一条件少于两个可用重复试次时，页面和导出均明确标记为仅探索性，不应把 p/q 值解释为确认性推断。

主要接口：

- `/api/recordings`：`FNIRS_DATA_DIR` 内的轻量 SNIRF 列表，可用 `subject` / `session` 过滤；不会触发分析
- `/api/uploads`（POST）：通过 `X-FNIRS-Filename` 和原始请求体上传 `.snirf` 到 `FNIRS_UPLOAD_DIR`
- `/api/uploads`（DELETE）：通过 `recording=<FNIRS_UPLOAD_DIR 内的相对路径>` 删除用户上传记录；内置样例返回 `403`
- `/api/recording`：记录摘要、UTF-8 `SubjectID` 显示名、任务参数、条件和 `inference_readiness`；该字段区分 GLM 计算状态与重复试次推断就绪度
- `/api/analysis-metadata`：分析清单，含输入 SHA-256、分析 ID、分析时间、UTF-8 `SubjectID` 显示名、临时兼容副本状态、Python/Cedalion/运行时依赖版本、参数、输入校验、逐通道排除原因和带推断就绪度的 GLM 摘要
- `/api/analysis-metadata-export`：下载同一份分析清单 JSON
- `/api/report-pdf`：下载当前记录的 PDF 分析报告
- `/api/probe`：探头几何校验、每通道距离及长/短间距分类
- `/api/auxiliary`：辅助流清单、选择、时间对齐审计、实际辅助/全局回归状态和拒绝原因
- `/api/quality`：逐通道 SNR/SCI 与筛选结果
- `/api/signal`：连续时间序列
- `/api/task-response`：任务平均、SEM、峰值及潜伏期
- `/api/task-export`：任务结果 CSV
- `/api/task-glm`：指定条件、通道和条件对比的 GLM 统计量；响应中的 `inference_readiness` 说明结果是推断就绪还是仅探索性
- `/api/task-glm-export`：所有 GLM 条件效应与条件对比的 CSV，并逐行保留推断就绪状态和原因

除 `/api/recordings` 外，所有读取、分析和导出接口都可加
`?recording=<FNIRS_DATA_DIR 内的相对 .snirf 路径>` 选择记录；省略时使用
`FNIRS_DEFAULT_FILE`。重复提供、目录外、非 SNIRF 或不存在的记录会返回明确的
`400` 或 `404` JSON 错误。

官方 `fingertapping.snirf` 的事件编号按源数据映射为 `1=Control`、`2=Tapping/Left`、`3=Tapping/Right`；首尾 `15` 作为边界标记排除。

其他数据若已经有文字形式的 `trial_type`，服务端会保留原标签。名称满足
`条件S` / `条件E` 的标记会按时间自动配对，例如 `wt2S` / `wt2E` 会生成
一个名为 `wt2` 的任务区间。没有结束标记的 `FS` 仍作为原始事件显示，但不会
进入任务分析。只有一个区间的条件只展示单次响应，不提供 SEM。GLM 仍可输出模型
估计，但会标为仅探索性，不作为重复试次推断结果；每条件两个可用试次只是当前最低
就绪门槛，不代表样本量或统计功效充分。

设备记录可能包含完整的光源×探测器矩阵，并以零填充未使用组合。服务端只纳入
每个波长至少 99% 采样为正值的通道，再仅在允许时间缺口内对入选通道的孤立非正值作
线性插值；端点或过长缺口对应的通道会退出分析，避免外推或对非正光强取对数。总览和
“质量与记录”页会同时显示原始与实际分析通道数。

## Docker

```bash
docker build -t fnirs-dashboard .
docker run -d --name fnirs-dashboard --restart unless-stopped \
  -p 10000:10000 fnirs-dashboard
```

如需分析自己的文件，将服务器目录只读挂载到 `/data`，并指定文件名：

```bash
docker run -d --name fnirs-dashboard --restart unless-stopped \
  -p 10000:10000 \
  -v /opt/fnirs-data:/data:ro \
  -e FNIRS_DEFAULT_FILE=recording.snirf \
  fnirs-dashboard
```

## 当前参数

- Prahl 消光系数
- DPF：默认 6，最小 0.01
- Butterworth 滤波：默认 0.01–0.5 Hz；下限设为 0 时仅执行上限低通
- TDDR 运动伪迹校正（任务分析）
- CBSI 血氧信号校正（连续信号可比较；默认关闭，可用于任务平均与 GLM）
- Wavelet 运动校正（仅作为连续信号实验比较选项，不与 TDDR 叠加）
- GVTD 运动异常候选检测
- GVTD 模式：默认 `report` 只标记；`exclude_epochs` 会从任务平均剔除包含异常采样的完整试次，并从 GLM 同步剔除异常采样
- 重采样：默认 `auto`；不规则时间轴以原始中位采样率规则化，目标采样率 `0` 表示自动。可设 `off` 保持严格拒绝，或用 `force` 重建规则时间轴。时间缺口默认不得超过 1 秒；下采样先使用 Cedalion 八阶 Butterworth 低通抗混叠。
- 分段：刺激前 5 秒至刺激后 20 秒；任务前窗口必须大于 0，网页设置的最小值为 0.5 秒
- 基线：刺激前 5 秒；只纳入基线和任务后窗均完整的区间；峰值响应窗为刺激后 3–15 秒
- SNR 阈值：2
- SCI 阈值：0.7，窗口 5 秒
- PSP：默认 5 秒窗口、PSP ≥ 0.1 且至少 75% 窗口合格，参与自动通道淘汰
- 短距离通道阈值：默认 15 mm，按 Cedalion 规则以 `distance < threshold` 分类
- 短距离通道模式：默认 `report`，也可显式设置为 `exclude`，仅从任务平均中排除
- GLM：默认 OLS、Gamma HRF `sigma=3 s`、余弦漂移截止 `0.003 Hz`、短间距回归模式 `auto`；计算成功与重复试次推断就绪度分开记录
- GLM 生理回归：默认 `off`；可显式启用辅助流、全局平均，或两者

科学参数在进程启动时从环境变量读取，未设置时使用上面的默认值。例如：

```bash
FNIRS_DPF=6 \
FNIRS_FILTER_MIN_HZ=0.01 FNIRS_FILTER_MAX_HZ=0.5 \
FNIRS_SNR_THRESHOLD=2 FNIRS_SCI_THRESHOLD=0.7 \
FNIRS_EPOCH_BEFORE_SECONDS=5 FNIRS_EPOCH_AFTER_SECONDS=20 \
FNIRS_RESPONSE_START_SECONDS=3 FNIRS_RESPONSE_END_SECONDS=15 \
FNIRS_SHORT_SEPARATION_THRESHOLD_MM=15 \
FNIRS_SHORT_SEPARATION_MODE=report \
FNIRS_GLM_NOISE_MODEL=ols \
FNIRS_GLM_DRIFT_CUTOFF_HZ=0.003 \
FNIRS_GLM_HRF_SIGMA_SECONDS=3 \
FNIRS_GLM_SHORT_SEPARATION_MODE=auto \
FNIRS_GLM_NUISANCE_MODE=off \
FNIRS_GLM_AUXILIARY_SIGNALS=heart_rate,respiration \
FNIRS_GLM_AUXILIARY_MAX_GAP_SECONDS=1 \
FNIRS_GVTD_MODE=report \
FNIRS_CBSI_MODE=off \
FNIRS_RESAMPLING_MODE=auto \
FNIRS_RESAMPLING_TARGET_RATE_HZ=0 \
FNIRS_RESAMPLING_MAX_GAP_SECONDS=1 \
FNIRS_DATA_DIR=/opt/fnirs-data \
FNIRS_DEFAULT_FILE=recording.snirf \
.venv/bin/python server.py --host 0.0.0.0 --port 10000
```

也可在“质量与记录 → 分析设置”中修改常用参数。网页设置会保存到
`FNIRS_DATA_DIR/.fnirs-dashboard-settings.json`，应用后立即用于重新分析，服务重启后
继续生效；“恢复服务器默认值”会删除该覆盖文件并重新采用启动时的环境变量配置。
可用 `FNIRS_ANALYSIS_SETTINGS_FILE` 指定其他保存路径。设置不会修改原始 SNIRF 文件。

`FNIRS_RESAMPLING_MODE` 支持 `off`、`auto` 和 `force`。`auto` 仅在采样间隔偏差
超过 1% 或设置了不同的目标采样率时启用；`force` 总会重建端点不变的规则时间轴；
`off` 会拒绝不规则输入。`FNIRS_RESAMPLING_TARGET_RATE_HZ=0` 采用原始中位采样率，
正数指定目标 Hz，且必须使目标 Nyquist 频率高于滤波上限。所有插值必须由相邻原始
时间点在 `FNIRS_RESAMPLING_MAX_GAP_SECONDS` 内包围，不能跨越长缺口或端点外推。
降采样先在规则源网格上执行 Cedalion 八阶 Butterworth 低通，再插值到目标网格；
刺激 `onset`/`duration` 保持秒语义。分析清单记录原始与目标采样率、样本数、缺口、
插值比例、抗混叠和事件时间审计。

还支持 `FNIRS_SCI_WINDOW_SECONDS`、`FNIRS_PSP_WINDOW_SECONDS`、
`FNIRS_PSP_THRESHOLD`（兼容旧名 `FNIRS_PSP_COMPUTATION_THRESHOLD`）、
`FNIRS_PSP_MIN_CLEAN_FRACTION`、`FNIRS_MIN_POSITIVE_FRACTION`、
`FNIRS_GEOMETRY_MIN_DISTANCE_MM` 和 `FNIRS_GEOMETRY_MAX_DISTANCE_MM`。
短距离通道还支持 `FNIRS_SHORT_SEPARATION_THRESHOLD_MM` 和
`FNIRS_SHORT_SEPARATION_MODE`（`report` 或 `exclude`）；简写环境变量
`FNIRS_SHORT_SEPARATION_MM` 也可使用。`report` 只在摘要中列出短距离通道，
`exclude` 只改变任务分段平均的可用通道，不改变连续信号和质量指标。
GLM 还支持 `FNIRS_GLM_NOISE_MODEL`（`ols` 或 `ar_irls`）、
`FNIRS_GLM_DRIFT_CUTOFF_HZ`、`FNIRS_GLM_HRF_SIGMA_SECONDS`、
`FNIRS_GLM_SHORT_SEPARATION_MODE`（`off` 或 `auto`）和
`FNIRS_GLM_AR_ORDER`。`auto` 仅在存在通过质量门限的短间距和长间距通道时，
为每个建模长通道加入最近短通道的回归量；否则保留原因但不伪造回归。默认 OLS
在 GLM 前仅低通，以余弦项处理慢漂移；`ar_irls` 不在拟合前做频率滤波，以保留
Cedalion 自回归预白化所需频谱，长记录的执行时间可能明显增加。
参数错误会在服务器启动时直接提示。探头距离上限默认只生成警告，不会自动删除通道。

`FNIRS_GLM_NUISANCE_MODE` 支持 `off`、`auxiliary`、`global` 与
`auxiliary_global`，默认 `off`。`auxiliary` 只使用
`FNIRS_GLM_AUXILIARY_SIGNALS` 明确列出的 `recording.aux_ts` 名称，绝不会按名称
猜测加速度计、PPG 或呼吸流的生理含义。每个选中流必须使用秒级时间轴、严格递增且完整
覆盖 GLM 时间范围；服务会限制插值括号缺口为
`FNIRS_GLM_AUXILIARY_MAX_GAP_SECONDS`，在源采样率高于 fNIRS 时先做四阶
Butterworth 低通抗混叠，再线性重采样并 z-score。无法安全对齐、非零 `time_offset`、
缺失、无方差或未选择的流都会被拒绝，并写入 GLM 状态和分析清单。

`global` 使用 Cedalion 的建模通道全局平均作为回归量，当前实现包含目标通道自身，故
只应在预先规定的研究方案中显式启用；`auxiliary_global` 会同时请求两种回归。三种模式
都只改变任务 GLM，不改变连续信号、质量指标或描述性任务平均。

`FNIRS_CBSI_MODE` 支持 `off` 和 `on`，默认 `off`。信号浏览始终提供 TDDR + CBSI
比较曲线；设为 `on` 后，任务平均和 GLM 使用 TDDR 后、按通道执行 CBSI 的
HbO/HbR。实现按 Cui 等（2010）以完整时间序列估计
`alpha = std(HbO) / std(HbR)`，再计算
`HbO' = (HbO - alpha * HbR) / 2` 和 `HbR' = -HbO' / alpha`。标准差比例不可安全
估计的通道保留原信号，并记录在分析清单中。CBSI 强制 HbO/HbR 负相关，正式研究应
仅在方案预先规定且该生理假设成立时启用。

`FNIRS_GVTD_MODE` 支持 `report` 和 `exclude_epochs`。默认 `report` 不改变任务或
GLM 结果。`exclude_epochs` 会剔除任务平均中包含 GVTD 异常采样的完整试次，并在 GLM
建立完整规则设计矩阵后同步删去异常采样行；清单会记录 GLM 的总采样、保留采样和删点数。
质量页可人工勾选“人工排除”通道，该决定
默认保存在 `FNIRS_DATA_DIR/.fnirs-dashboard-qc.json`，可用
`FNIRS_QC_DECISIONS_FILE` 指定其他可写路径。人工排除会从任务平均和 GLM 中移除通道，
并进入新的分析 ID、分析清单和导出关联信息；请将该决定文件作为研究审计资料保留。

每次记录摘要都会包含 `analysis_parameters` 和 `input_validation`。服务端会在
OD 计算前检查波长、时间轴、刺激列、源/探测器坐标和源-探测器距离；无单位的
原始光强会明确记录并按相对电压参与 OD 比值计算。

服务端会在实际分析通道上识别短距离通道，并在摘要中列出标签、光极和距离。任务
平均仍是分段平均；GLM 的 `auto` 模式会在条件满足时使用最近的合格短间距通道作为
长通道协变量，且只影响 GLM，不改变连续信号、质量指标或任务平均。SNIRF 的
`recording.aux_ts` 会清单化名称、形状、单位、采样率、时间覆盖和有限值比例。只有配置
明确选择且通过时间对齐审计的辅助流会进入 GLM；`time_offset` 不会被自动应用。

这些参数适合先打通演示链路，正式分析时应根据设备、受试者和研究方案配置。

## 可复现性

每份成功分析都有稳定的 `analysis_id`，它由输入 SHA-256、分析参数、分析协议版本和
运行时软件版本计算。`/api/analysis-metadata` 及总览页“下载分析清单”会输出同一份
JSON，记录输入文件、软件版本、完整参数、时间轴重采样审计、光强和质量筛选排除、探头/短间距状态、
运动摘要、任务设置和 GLM 设置。清单的 `created_at_utc` 表示该文件当前缓存版本首次
完成分析的时间；输入文件大小或修改时间变化后会重新分析并生成新的清单。
