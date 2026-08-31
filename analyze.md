# 分析 Graphite vs Ganesh 测试结果

## 前置条件

测试结果由 `graphite_vs_ganesh.py run` 生成，存放在 `results-<GPU>-<Driver>-<Resolution>-<CPU>-<Timestamp>` 目录中。

## 使用方法

```bash
cd d:\graphiteperf
```

### 生成 HTML 报告（推荐）

```bash
# 所有指标，生成交互式 HTML
python3 analyze_results.py <results_dir> --all-metrics --html report.html

# 仅关键指标
python3 analyze_results.py <results_dir> --html report.html
```

### 生成 CSV（用于 Excel 分析）

```bash
python3 analyze_results.py <results_dir> --all-metrics --csv report.csv
```

### 控制台查看

```bash
# 默认关键指标，5% 阈值
python3 analyze_results.py <results_dir>

# 只看回归项，10% 阈值
python3 analyze_results.py <results_dir> --only-regressions --threshold 10

# 看特定指标
python3 analyze_results.py <results_dir> --metric motionmark
python3 analyze_results.py <results_dir> --metric thread_GPU

# 列出所有可用指标
python3 analyze_results.py <results_dir> --list-metrics
```

## 参数说明

| 参数 | 说明 |
|------|------|
| `--html <file>` | 输出交互式 HTML 报告 |
| `--csv <file>` | 输出 CSV 文件 |
| `--metric <name>` | 按指标名子串过滤 |
| `--all-metrics` | 显示所有指标（默认只显示关键指标） |
| `--only-regressions` | 只显示回归项 |
| `--threshold <N>` | 回归/改进判定阈值，默认 5% |
| `--list-metrics` | 列出所有可用指标名 |

## 关键指标含义

| 指标 | 方向 | 说明 |
|------|------|------|
| `cpu_wall_time_ratio` | 越大越好 | CPU 利用率 |
| `thread_GPU_cpu_time_per_frame` | 越小越好 | GPU 线程每帧 CPU 时间 |
| `thread_total_all_cpu_time_per_frame` | 越小越好 | 所有线程每帧总 CPU 时间 |
| `thread_total_rendering_cpu_time_per_frame` | 越小越好 | 渲染相关线程每帧 CPU 时间 |
| `thread_raster_cpu_time_per_frame` | 越小越好 | 光栅化线程每帧 CPU 时间 |
| `thread_renderer_main_cpu_time_per_frame` | 越小越好 | 渲染主线程每帧 CPU 时间 |
| `thread_renderer_compositor_cpu_time_per_frame` | 越小越好 | 合成器线程每帧 CPU 时间 |
| `thread_display_compositor_cpu_time_per_frame` | 越小越好 | 显示合成器每帧 CPU 时间 |
| `thread_browser_cpu_time_per_frame` | 越小越好 | 浏览器线程每帧 CPU 时间 |
| `motionmark` | 越大越好 | MotionMark 基准分数 |
| `queueing_durations` | 越小越好 | 队列等待时间 |

## 结果解读

- **Diff%**：`(Graphite - Ganesh) / |Ganesh| × 100%`
- **REGRESS**：Graphite 相对 Ganesh 性能变差（超过阈值）
- **IMPROVE**：Graphite 相对 Ganesh 性能变好（超过阈值）
- **neutral**：差异在阈值范围内

## 示例

```bash
# MTL 设备结果
python3 analyze_results.py results-Intel(R)_Arc(TM)_Graphics-32.0.101.8737-2880x1800-Intel(R)_Core(TM)_Ultra_9_185H-20260519_135713_results --all-metrics --html results_comparison.html
```
