# WPR (Web Page Replay) 笔记

## 1. WPR 是什么

WPR = **Web Page Replay**，Chromium 用来做"录制-回放"网页流量的工具（源码在
`chromium/src/third_party/webpagereplay`，Go 写的）。

- **录制**时，启动一个本地代理，把某个页面真实访问过程中所有的 HTTP 响应（HTML/JS/CSS/图片等）
  打包进一个 `.wprgo` 归档文件。
- **回放**时，Telemetry/`run_benchmark` 启动 WPR 代理拦截浏览器的网络请求，直接从归档文件里吐出
  录制时的原始响应，不再真正连外网。

好处：每次跑 benchmark，页面内容、资源大小、响应时序都是完全一致的字节，跑分才有可比性、可复现性，
不受网络抖动、站点改版、广告轮播等因素干扰。

## 2. `run_benchmark` 默认行为 vs `--use-live-sites`

`run_benchmark` 默认会用 WPR 回放（见下）。可以加 `--use-live-sites` 让浏览器直接访问真实网站，
跳过 WPR。

| | 默认（WPR 回放） | `--use-live-sites` |
|---|---|---|
| 网络请求 | 走本地 WPR 代理，返回录制时的固定内容 | 真实连外网，访问当前的真实站点 |
| 是否需要归档文件 | 需要（本地已有，或从 GCS bucket 下载） | 不需要 |
| 内容一致性 | 每次完全一致（可复现、跨机器可比） | 站点内容可能变化，引入额外噪声 |
| 网络抖动影响跑分 | 无（本地回放） | 有 |
| 权限要求 | 需要能访问归档所在的 storage | 无 |
| 典型用途 | CI 上做稳定的性能回归对比 | 本地没有归档访问权限时的应急方案，或想测"当前真实网页" |

### 我们实际踩过的坑

`rendering.desktop` 的部分 story（如 `wikipedia_2018`）默认会去 GCS bucket
`chrome-partner-telemetry`（Google 内部 bucket）下载归档，如果没有 `gcloud auth login` /
没有权限，会报：

```
CredentialsError: Attempted to access a file from Cloud Storage but you have no
configured credentials ...
```

调用栈关键点：`story_runner.RunStorySet` → `_UpdateAndCheckArchives` →
`wpr_archive_info.DownloadArchivesIfNeeded` → `cloud_storage.GetIfChanged`。

## 3. 怎么根据一个网址反查它对应的 `.wprgo` 归档文件

不是直接从 URL 反查文件，是通过 story 名字（`BASE_NAME`）做中间桥梁，分两步：

### 第 1 步：从 URL 找到 story 定义，拿到 `BASE_NAME`

在 `tools/perf/page_sets/` 下全文搜这个 URL：

```bash
grep -r "ie.microsoft.com" chromium/src/tools/perf/page_sets
```

例如命中 `tools/perf/page_sets/rendering/tough_canvas_cases.py`：

```python
class MicrosoftWorkerFountainsPage(ToughCanvasPage):
  BASE_NAME = 'microsoft_worker_fountains'
  URL = 'http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html'
```

每个 story 类都有 `URL`（录制/回放时访问的真实网址）和 `BASE_NAME`（story 的唯一标识，也就是
`run_benchmark ... --story=xxx` 里用的名字）。

### 第 2 步：用 `BASE_NAME` 查归档索引

每个 benchmark 的 `page_sets/data/<benchmark>.json` 是 story 名到 wprgo 文件的映射表（文件里写着
"Don't edit by hand! Use record_wpr for updating"）：

```bash
grep -A2 "microsoft_worker_fountains" chromium/src/tools/perf/page_sets/data/rendering_desktop.json
```

```json
"microsoft_worker_fountains": {
    "DEFAULT": "rendering_desktop_004.wprgo"
}
```

多个 story 经常共用同一个 wprgo（一个归档里录了一整批页面）。

## 4. 不走 `run_benchmark`，直接用浏览器打开 wprgo 归档

原理：WPR 只是一个通用的"URL → 录制响应"代理，跟 `run_benchmark` 没有绑定关系。手动步骤：

1. **wpr 工具源码**：`chromium/src/third_party/webpagereplay`（DEPS 里的公开 git 依赖，本仓库已经
   同步好了，包含 Go 源码 + 证书 + bundle 的 go 工具链）。
   - 启动脚本实际在 **`scripts/run_wpr.py`**（不是仓库根目录，容易踩坑）。
   - 证书 `wpr_key.pem` / `wpr_cert.pem`、`deterministic.js` 在仓库根目录。
   - Go 工具链在 `third_party/golang/win/x64/bin/go.exe`，`run_wpr.py` 每次调用都会用它现场
     `go build` 出 `wpr.exe`（本地 module cache 已有依赖，不需要联网）。

2. **起 replay 代理**：
   ```
   python third_party\webpagereplay\scripts\run_wpr.py replay ^
     --http_port=0 --https_port=0 ^
     --https_key_file=third_party\webpagereplay\wpr_key.pem ^
     --https_cert_file=third_party\webpagereplay\wpr_cert.pem ^
     --inject_scripts=third_party\webpagereplay\deterministic.js ^
     tools\perf\page_sets\data\rendering_desktop_004.wprgo
   ```
   端口填 `0` 表示让系统自动分配，实际端口会打印在日志里：
   ```
   Starting server on http://127.0.0.1:52834
   Starting server on https://127.0.0.1:52835
   ```

3. **用普通 Chrome 打开，走这个代理**：
   ```
   chrome.exe --proxy-server="http=127.0.0.1:52834;https=127.0.0.1:52835" ^
     --ignore-certificate-errors --user-data-dir=C:\temp\wpr_profile
   ```
   （`--ignore-certificate-errors` 是因为 wpr 用自签证书做 HTTPS 中间人解密。）

4. **访问归档里录制时的原始 URL**（scheme/host/path 必须一致，比如这里是
   `http://` 不是 `https://`）：
   ```
   http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html
   ```

### 注意事项

- WPR 起来后会接管**所有**域名的流量；只有归档里录制过的资源能正常返回，其余请求都会失败——这是
  预期行为。
- URL 必须和录制时完全一致（含 scheme），否则 WPR 会报 "no matching response"。

## 5. 封装脚本：`wpr.py`

`D:\graphiteperf\wpr.py` 把上面第 4 节的手动步骤封装成了一条命令：自动起 wpr replay、
解析实际分配到的端口、拼好 Chrome 的 `--proxy-server`，用完按回车自动清理。

用法：
```
python wpr.py --archive chromium\src\tools\perf\page_sets\data\rendering_desktop_004.wprgo --url http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html
```

参数：
- `--archive`（必填）：`.wprgo` 归档路径
- `--url`（必填）：归档里录制的原始 URL（第 3 节的方法找）
- `--chrome`：Chrome 可执行文件路径，默认
  `C:\Users\<当前登录用户>\AppData\Local\Google\Chrome SxS\Application\chrome.exe`
  （通过 `getpass.getuser()` 自动取当前用户名）
- `--args`：额外 Chrome flags，默认 `--disable-skia-graphite --show-fps-counter`
- `--wpr-dir`：webpagereplay checkout 路径，默认
  `D:\graphiteperf\chromium\src\third_party\webpagereplay`
- `--http-port` / `--https-port`：默认 `0`（自动分配）
- `--user-data-dir`：Chrome profile 目录，默认临时目录

如果 `--wpr-dir` 下找不到 `scripts\run_wpr.py`，脚本会报错并提示去
`chromium\src` 下 `gclient sync`。

## 6. 怎么修改 Chrome 参数

`--args` 接收一整个字符串，脚本内部用 `shlex.split()` 按空格拆成多个 flag 再传给 Chrome，
所以你直接用 `--args` 整体覆盖默认值即可，不是追加。

比如默认是 `--disable-skia-graphite --show-fps-counter`（跑 Ganesh），想改成跑
Graphite（`--enable-skia-graphite`），在 bash 里用引号把整串包起来：

```bash
python wpr.py \
  --archive chromium/src/tools/perf/page_sets/data/rendering_desktop_004.wprgo \
  --url http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html \
  --args "--enable-skia-graphite --show-fps-counter"
```

Windows `cmd.exe` 下同理（用双引号）：

```bat
python wpr.py ^
  --archive chromium\src\tools\perf\page_sets\data\rendering_desktop_004.wprgo ^
  --url http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html ^
  --args "--enable-skia-graphite --show-fps-counter"
```

注意：
- 引号必须把多个 flag 包成一个字符串传给 `--args`，不要写成
  `--args --enable-skia-graphite --show-fps-counter`（不加引号的话 argparse 会把
  `--show-fps-counter` 当成一个新的、未定义的选项而报错）。
- 想同时对比 Ganesh/Graphite，起两次脚本，分别用不同的 `--args` 和 `--user-data-dir`
  （避免共用 profile 导致的干扰）即可。
