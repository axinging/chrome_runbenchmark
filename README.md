# Graphite vs Ganesh Performance Benchmark

比较 Chrome 在 Graphite 和 Ganesh 渲染后端下的性能表现。

## 文件说明

- `setup_chromium_env.py` — 环境搭建脚本（安装工具、下载代码）
- `graphite_vs_ganesh_dropped.py` — 性能测试脚本（运行 rendering.desktop benchmark）
- `config.json` — **本地配置文件**（代理等机器相关配置），不入 git
- `config.example.json` — 配置模板，复制为 `config.json` 后按需修改

## 零、本地配置（config.json）

代理地址不再硬编码在脚本里，改为从脚本同目录下的 `config.json` 读取。

```shell
copy config.example.json config.json
```

然后编辑 `config.json`：

```json
{
  "proxy": "http://proxy.example.com:911"
}
```

| 字段 | 说明 |
|---|---|
| `proxy` | HTTP/HTTPS 代理地址。留空字符串 `""` 或删除该字段表示不使用代理 |

读取该配置的脚本：

| 脚本 | 用途 | 行为 |
|---|---|---|
| `setup_chromium_env.py` | 给 git / gclient / fetch 设置代理 | `--proxy` 的**默认值**即 `config.json` 里的 `proxy`；`--proxy=<url>` 可覆盖，`--proxy=` 可禁用 |
| `graphite_vs_ganesh_dropped.py` | 给 Chrome 设置 `--proxy-server` | 默认**不启用**代理，需显式传 `--proxy`（不带值时暂不会自动读取 config，见下方注意事项） |

**容错**：`config.json` 不存在或格式错误时，脚本不会崩溃——代理按"未配置"处理（即不使用代理），格式错误会额外打印一条 warning。

**不要提交 `config.json`**：已在 `.gitignore` 中忽略，避免把内网代理地址推到远端仓库。

## 一、环境搭建（新机器首次使用）

### 前置要求

- Windows 10+
- 已安装 [Git for Windows](https://git-scm.com/download/win)
- 已安装 [Visual Studio 2022+](https://visualstudio.microsoft.com/) (含 "Desktop development with C++" 组件)
- 已安装 [Chrome Canary](https://www.google.com/chrome/canary/)
- 已安装 [Python 3](https://www.python.org/downloads/)
- 至少 100GB 可用磁盘空间（NTFS）

> **Windows `python` 命令问题**：如果运行 `python` 跳转到 Microsoft Store，需禁用 App Execution Aliases：
> ```powershell
> # PowerShell（管理员）
> Remove-Item "$env:LOCALAPPDATA\Microsoft\WindowsApps\python.exe" -ErrorAction SilentlyContinue
> Remove-Item "$env:LOCALAPPDATA\Microsoft\WindowsApps\python.exe" -ErrorAction SilentlyContinue
> ```
> 或打开 **设置 → 应用 → 应用执行别名**，关闭 `python.exe` 和 `python.exe` 的开关。

### 运行搭建脚本

```shell
python setup_chromium_env.py
```

默认会在当前目录下创建 `chromium/` 文件夹存放源码（即 `./chromium/src/`）。

脚本会自动完成：
1. 检查 Git
2. 克隆 depot_tools 并加入 PATH
3. 配置 Git（autocrlf、longpaths 等）
4. 执行 `fetch --no-history chromium`（无历史记录，最小化下载）
5. 安装 Python 依赖

### 常用选项

```shell
# 使用 config.json 里配置的代理（默认行为，无需额外参数）
python setup_chromium_env.py

# 临时覆盖 config.json 里的代理
python setup_chromium_env.py --proxy=http://proxy.example.com:911

# 本次不使用代理（不改 config.json）
python setup_chromium_env.py --proxy=

# 自定义目录
python setup_chromium_env.py --depot-tools-dir C:\src\depot_tools --chromium-dir D:\cr\chromium

# 代码已下载，只配置环境
python setup_chromium_env.py --skip-fetch

# 跳过 git 全局配置
python setup_chromium_env.py --skip-git-config
```

如果要支持WPR，setup_chromium_env注释掉webpagereplay：
# Other
# if no use-live site: src\\third_party\\webpagereplay\\scripts\\run_wpr.py': [Errno 2] No such file or directory
#'src/third_party/webpagereplay',

然后在.gclient 删除webpagereplay相关行。重新运行python setup_chromium_env.py --proxy=


git bash:
export BOTO_CONFIG=$(gcloud info --format "value(config.paths.global_config_dir)")/legacy_credentials/$(gcloud config list --format="value(core.account)")/.boto

gcloud auth login





## 二、运行性能测试

进入 graphiteperf 目录：

```shell
cd D:\cr\chromium\src\graphiteperf
```

### 1. 打补丁（首次下载代码后执行一次）

```shell
python graphite_vs_ganesh_dropped.py patch
```

这会对 `third_party/catapult` 应用两个修复：
- 注释掉 `--no-proxy-server` 断言（允许自定义代理）
- 添加 `use_live_traffic` 时跳过 tsproxy（让 Chrome 使用我们的企业代理）
- 修补 websocket 客户端，确保 DevTools 连接不走代理

### 2. 初始化（获取可用 story 列表）

```shell
python graphite_vs_ganesh_dropped.py init
```

这会安装 Python 依赖并将所有 rendering.desktop story 保存到 `stories.json`。

### 3. 运行测试

```shell
# 运行单个 story，同时测试 Graphite 和 Ganesh
python graphite_vs_ganesh_dropped.py run --story=wikipedia_2018

# 只测试 Graphite
python graphite_vs_ganesh_dropped.py run --story=wikipedia_2018 --mode=graphite

# 只测试 Ganesh
python graphite_vs_ganesh_dropped.py run --story=wikipedia_2018 --mode=ganesh

# 运行所有 story（需先 init）
python graphite_vs_ganesh_dropped.py run

# 访问 live sites 需要走代理时，显式指定
python graphite_vs_ganesh_dropped.py run --story=youtube_2018 --proxy=http://proxy.example.com:911

# 明确禁用代理（本地 story / 直连网络）
python graphite_vs_ganesh_dropped.py run --story=wikipedia_2018 --no-proxy
```

python graphite_vs_ganesh_dropped.py run --story=main_15fps_with_jank_impl_0fps


[text](../www/page_sets/simple_canvas/falling_particle_simulation_gpu_fix.html)

### 4. 查看结果

测试完成后，结果保存在 `run_results.json`，终端也会打印 PASS/FAIL 汇总。

### 5. 分析结果

```shell
# 默认分析（包含 FPS + DF3/DF4 + 所有 metrics，输出 HTML 报告）
python analyze_results.py <results_dir>

# 只看关键 metrics
python analyze_results.py <results_dir> --no-all-metrics

# 禁用 FPS 提取
python analyze_results.py <results_dir> --no-fps

# 禁用 DF3/DF4 提取
python analyze_results.py <results_dir> --no-df

# 导出 CSV
python analyze_results.py <results_dir> --csv output.csv
```

#### FPS vs DF3/DF4 的区别

两者都从同一份 trace 文件提取，但衡量角度不同：

| | FPS | DF3/DF4 (PercentDroppedFrames) |
|---|---|---|
| **来源** | `BenchmarkInstrumentation::DisplayRenderingStats` 帧边界事件 | `Graphics.Smoothness.PercentDroppedFrames3/4` HistogramSample 事件 |
| **计算方式** | 相邻帧边界时间差 → `1000 / avg_frame_time` | Chrome FrameSequenceTracker 统计的丢帧百分比 |
| **衡量什么** | **吞吐量** — 每秒输出多少帧 | **流畅度** — 多少帧错过了 VSync deadline |
| **理想值** | 60（或显示器刷新率） | 0% |
| **参数** | `--no-fps` 禁用 | `--no-df` 禁用 |



**avg_frame_time 计算过程**：
1. 从 trace 文件提取所有 `BenchmarkInstrumentation::DisplayRenderingStats` 事件的 `ts`（微秒时间戳）
2. 排序后取相邻时间戳差值，除以 1000 转为毫秒：`frame_time_ms = (ts[i+1] - ts[i]) / 1000`
3. 算术平均：`avg_frame_time = sum(frame_times_ms) / count`
4. FPS = `1000 / avg_frame_time`

**关系示例**：
- 60Hz 满帧：FPS=60, DF=0%
- 稳定 30 FPS：FPS=30, DF≈50%（每隔一帧丢一帧）
- 偶尔卡顿：FPS≈58, DF=5-10%（平均 FPS 看不出来，但 DF 能反映卡顿）

**简单说**：FPS 告诉你"跑了多快"，DF 告诉你"有多卡"。

## 注意事项

### 代理相关

- **不是所有脚本都需要代理。** 只有两类操作需要：
  1. `setup_chromium_env.py` 下载 depot_tools / Chromium 源码（走 git、gclient）
  2. benchmark 访问 **live sites**（`--use-live-sites`，如 youtube_2018、wikipedia_2018）

  其余场景一般 **不需要** 代理，例如：
  - 用本地 HTTP server 跑 story（`serve_stories.py` + `stories_local_http.json`）
  - 纯本地的分析脚本（`analyze_results.py` 等），它们只读磁盘上的 trace/结果文件
  - 机器本身可直连外网（家庭网络、非内网环境）

  这些情况下 `config.json` 可以不创建，或把 `proxy` 设成 `""`。

- 代理只对本地回环地址例外：脚本会自动设置 `no_proxy=localhost,127.0.0.1`，
  并给 Chrome 加 `--proxy-bypass-list=localhost;127.0.0.1;<local>`，
  否则 DevTools WebSocket 连接会被代理拦掉、benchmark 直接失败。

- 两个脚本的默认值**不一样**，容易踩坑：
  - `setup_chromium_env.py`：默认**读 `config.json` 并启用**代理
  - `graphite_vs_ganesh_dropped.py`：默认**不启用**代理（`PROXY = ''`），
    需要时用 `--proxy=<url>` 显式指定，`--no-proxy` 显式关闭

- 走代理跑 live sites 前，必须先执行过 `patch` 命令（见"二、运行性能测试 → 1. 打补丁"），
  否则 Telemetry 的 tsproxy 会覆盖掉自定义代理，表现为页面打不开。详见 `proxy.md`。

- `config.json` 里是内网地址，**不要提交到 git**（已在 `.gitignore` 中）。

### 其他

- 默认使用 Chrome Canary（`%LOCALAPPDATA%\Google\Chrome SxS\Application\chrome.exe`）
- Graphite 使用 Dawn D3D11 后端（`--skia-graphite-backend=dawn-d3d11`）



可以。有两种方式：

### 1. `custom_deps` — 跳过子仓库（DEPS 依赖）

修改 .gclient 文件（在 .gclient），将不需要的 deps 设为 `None`：

```python
solutions = [
  {
    "name": "src",
    "url": "https://chromium.googlesource.com/chromium/src.git",
    "custom_deps": {
      # 跳过不需要的大型依赖
      "src/third_party/angle": None,
      "src/ios/third_party": None,
      "src/chrome/test/data": None,
      "src/third_party/hunspell_dictionaries": None,
      "src/native_client": None,
    },
  },
]
```

### 2. Git sparse-checkout — 跳过主仓库中的文件夹

```shell
cd D:\cr\chromium\src
git sparse-checkout init --cone
git sparse-checkout set tools/perf third_party/catapult build graphiteperf
```

这样只检出指定目录，其余文件夹为空。

---

### 对你的场景建议

graphite_vs_ganesh_dropped.py 运行只需要：
- `tools/perf/` — benchmark runner
- `third_party/catapult/` — telemetry framework
- `build/` — 部分构建工具

可以在 `setup_chromium_env.py` 中加入 sparse-checkout 支持。需要我加这个功能吗？



python analyze_results.py "results-a" --all-metrics --html results_comparison_mtl_120hz.html