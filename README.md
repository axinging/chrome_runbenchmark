# Graphite vs Ganesh Performance Benchmark

比较 Chrome 在 Graphite 和 Ganesh 渲染后端下的性能表现。

## 文件说明

- `setup_chromium_env.py` — 环境搭建脚本（安装工具、下载代码）
- `graphite_vs_ganesh_dropped.py` — 性能测试脚本（运行 rendering.desktop benchmark）

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
# 指定代理（默认已配置代理）
python setup_chromium_env.py --proxy=http://proxy.com:11

# 不使用代理
python setup_chromium_env.py --proxy=

# 自定义目录
python setup_chromium_env.py --depot-tools-dir C:\src\depot_tools --chromium-dir D:\cr\chromium

# 代码已下载，只配置环境
python setup_chromium_env.py --skip-fetch

# 跳过 git 全局配置
python setup_chromium_env.py --skip-git-config
```

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

- 需要网络代理才能访问 live sites（已在 Chrome 启动参数中配置）
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