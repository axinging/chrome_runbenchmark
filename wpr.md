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

每个 story 类都有 `URL`（录制/回放时访问的真实网址）和 `BASE_NAME`。**注意：`BASE_NAME` 不一定
就是 `run_benchmark ... --story=xxx` 里用的完整名字**，见下面「`BASE_NAME` 和真实 story 名不一致的坑」。

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

### `BASE_NAME` 和真实 story 名不一致的坑（`YEAR` 后缀）

`page_sets/rendering/rendering_story.py` 里 `RenderingStory.__init__` 实际拼出来的
story 名是：

```python
name = self.BASE_NAME + name_suffix
if self.YEAR:
  name += '_' + self.YEAR
```

也就是说，如果某个 story 类定义了 `YEAR`，真实 story 名 = `BASE_NAME + '_' + YEAR`，
**不是**裸的 `BASE_NAME`。例如 `tough_pinch_zoom_cases.py`：

```python
class EBayPinchZoom2018Page(ToughPinchZoomPage):
  BASE_NAME = 'ebay_pinch'
  YEAR = '2018'
  URL = 'http://www.ebay.com'
```

真实 story 名是 `ebay_pinch_2018`，不是 `ebay_pinch`。`page_sets/data/rendering_desktop.json`
里的归档索引键、以及 `run_benchmark --story=` 认的都是 `ebay_pinch_2018`：

```
grep -n "ebay" chromium/src/tools/perf/page_sets/data/rendering_desktop.json
102:        "ebay_2018": {
105:        "ebay_pinch_2018": {
```

排查方法：如果 `--story=<BASE_NAME>` 报 `No story with BASE_NAME = "..." found`，或者
（用 `run_benchmark`）报 `--story` 找不到，先看看对应的 story 类里是不是有 `YEAR` 字段，
真实名字大概率是 `BASE_NAME_YEAR`。`wpr.py` 已经修复了这个问题（见下方「用法 2」），
会同时按 `BASE_NAME` 和 `BASE_NAME_YEAR` 两种可能去匹配，不需要手动拼。

### 第 3 步（可选）：直接在归档里搜，确认这个 URL 真的被录进去了

`page_sets/data/*.json` 只能告诉你 story 对应哪个 `.wprgo` 文件，并不能证明具体某个 URL 真的被
录进了那个文件里。想要更直接的证据，`.wprgo` 本质是一个 gzip 压缩的归档，可以解压后直接搜字符串：

```bash
gunzip -c chromium/src/tools/perf/page_sets/data/rendering_desktop_003.wprgo \
  | grep -a -o "https\?://testdrive-archive.azurewebsites.net[^\"[:space:]]*" | sort -u
```

能搜到目标 URL（以及它的子资源，如 CSS/字体/图片/JS）说明确实录进去了：

```
https://testdrive-archive.azurewebsites.net/performance/chalkboard/
https://testdrive-archive.azurewebsites.net/performance/chalkboard/Audio/Whoosh2.mp3
https://testdrive-archive.azurewebsites.net/performance/chalkboard/Chalkboard.css
...
```

另外，`wpr.py` 起 WPR 时打印的 `ScriptInjector succesfully injected url=...` 日志（见第 7 节
`ie_chalkboard` 例子）本身也是间接证据——wpr 只会对归档里真实存在的请求做脚本注入，日志里能看到
目标 URL 就说明它在归档里。

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

`wpr.py` 默认假设自己跟 `chromium` checkout 放在同一个父目录下，即：
```
<某目录>/wpr.py
<某目录>/chromium/src/...
```
所有默认路径（`--chromium-src`、`--wpr-dir`）都是相对脚本自身位置（`Path(__file__).resolve().parent`）
算出来的，不是写死的绝对路径，所以整个目录可以随意挪动/改名。

### 用法 1：手动指定 URL + 归档

```
python wpr.py --archive chromium\src\tools\perf\page_sets\data\rendering_desktop_004.wprgo --url http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html
```

### 用法 2：只给 story 的 `BASE_NAME`，自动查 URL 和归档

不想自己按第 3 节的方法手动 grep 的话，直接给 `--story`，`wpr.py` 会按同样的逻辑自动查：

```
python wpr.py --story microsoft_worker_fountains
```

输出里会打印实际解析出的 URL / 归档路径，例如：
```
[story] --story=microsoft_worker_fountains -> --url=http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html  (from .../tough_canvas_cases.py)
[story] --story=microsoft_worker_fountains -> --archive=.../rendering_desktop_004.wprgo  (from .../rendering_desktop.json)
```

如果同一个 story 在多个 benchmark 的索引里都有记录（比如同时存在于
`rendering_desktop.json` 和 `rendering_mobile.json`，对应不同归档），`wpr.py` 默认优先选
文件名里带 "desktop" 的那个；如果还是无法唯一确定，会报错并列出所有候选，此时用
`--benchmark <benchmark名>`（不带 `.json` 后缀，比如 `--benchmark rendering_desktop`）
显式指定用哪个索引。

`--url` / `--archive` 只要手动给了，就会直接采用，不会走 `--story` 自动解析（即显式参数优先）。

`YEAR` 后缀的例子（`ebay_pinch_2018`）：
```
python wpr.py --story ebay_pinch_2018
```
```
[story] --story=ebay_pinch_2018 -> --url=http://www.ebay.com  (from .../tough_pinch_zoom_cases.py)
[story] --story=ebay_pinch_2018 -> --archive=.../rendering_desktop_f083c0f144.wprgo  (from .../rendering_desktop.json)
```
如果传的是裸 `BASE_NAME`（`--story ebay_pinch`，少了 `_2018`），`--url` 能解析出来（因为
`wpr.py` 对 `BASE_NAME` 和 `BASE_NAME_YEAR` 都认），但 `--archive` 会报错找不到归档映射——
因为 `page_sets/data/*.json` 里的索引键是完整的 `ebay_pinch_2018`，没有 `ebay_pinch` 这个键。
报错信息会明确提示 "No wprgo archive mapping for story ..."，此时把 `--story` 换成完整名字
（带 `_YEAR` 后缀）即可。

参数：
- `--archive`：`.wprgo` 归档路径。给了 `--story` 时可省略，会自动解析
- `--url`：归档里录制的原始 URL（第 3 节的方法找）。给了 `--story` 时可省略，会自动解析
- `--story`：page_sets story 的真实名字，例如 `microsoft_worker_fountains`；用来自动解析
  `--url`/`--archive`。大多数 story 真实名字就是 `BASE_NAME`，但少数定义了 `YEAR` 的 story
  真实名字是 `BASE_NAME_YEAR`（例如 `ebay_pinch_2018`，见第 3 节「`BASE_NAME` 和真实 story
  名不一致的坑」）——`wpr.py` 会同时按两种可能匹配，不需要手动区分
- `--chromium-src`：chromium/src checkout 路径，`--story` 解析时用；默认
  `<wpr.py 所在目录>\chromium\src`
- `--benchmark`：`--story` 对应的归档存在于多个 benchmark 索引里、无法自动消歧时，用它
  显式指定用哪个（对应 `tools\perf\page_sets\data\<benchmark>.json`）
- `--chrome`：Chrome 可执行文件路径，默认
  `C:\Users\<当前登录用户>\AppData\Local\Google\Chrome SxS\Application\chrome.exe`
  （通过 `getpass.getuser()` 自动取当前用户名）
- `--args`：额外 Chrome flags，默认 `--disable-skia-graphite --show-fps-counter`
- `--wpr-dir`：webpagereplay checkout 路径，默认 `<--chromium-src>\third_party\webpagereplay`
- `--http-port` / `--https-port`：默认 `0`（自动分配）
- `--user-data-dir`：Chrome profile 目录，默认临时目录

### 报错情况

- 如果解析/指定出来的 `.wprgo` 归档文件在磁盘上不存在（无论是手动传的 `--archive` 还是
  `--story` 自动解析出来的），会直接报错并提示去跑一次真实的 `run_benchmark` 让它自动下载，
  或者手动把 `.wprgo` 文件拷过来，不会静默失败或者跑到一半才崩。
- 如果 `--wpr-dir` 下找不到 `scripts\run_wpr.py`，脚本会报错并提示去
  `chromium\src` 下 `gclient sync`。
- 想看完整参数说明和示例，直接 `python wpr.py --help`（内置了详细的 examples / how it works /
  --story resolution 说明）。

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

## 7. 两种模式的完整示例 + 验证过程

`wpr.py` 支持两种互斥的使用模式：**手动模式**（自己给 `--archive`/`--url`）和 **story 模式**
（给 `--story`，自动查）。下面各给一个完整跑过的例子，以及当初验证脚本改动时实际执行、确认可用的过程。

### 模式 1：手动模式（自己知道归档和 URL）

适用场景：已经按第 3 节的方法手动 grep 出了 URL 和归档，或者是自己录的、不在 page_sets 索引里的
归档。

```bash
python wpr.py \
  --archive chromium/src/tools/perf/page_sets/data/rendering_desktop_004.wprgo \
  --url http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html
```

这种模式下 `wpr.py` 完全不碰 page_sets 索引，直接拿这两个值起 WPR + Chrome。

### 模式 2：story 模式（只知道 story 名字，让脚本自动查）

适用场景：知道 benchmark 里的 story 名（`BASE_NAME`），懒得自己去 grep URL 和归档索引。

```bash
python wpr.py --story microsoft_worker_fountains
```

脚本内部按第 3 节同样的逻辑自动解析，并把解析结果打印出来（不是静默做的）：

```
[story] --story=microsoft_worker_fountains -> --url=http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html  (from D:\graphiteperf\chromium\src\tools\perf\page_sets\rendering\tough_canvas_cases.py)
[story] --story=microsoft_worker_fountains -> --archive=D:\graphiteperf\chromium\src\tools\perf\page_sets\data\rendering_desktop_004.wprgo  (from D:\graphiteperf\chromium\src\tools\perf\page_sets\data\rendering_desktop.json)
```

如果这个 story 同时存在于多个 benchmark 索引（比如 desktop 和 mobile 都收录了它，对应不同归档），
补一个 `--benchmark` 消歧：

```bash
python wpr.py --story some_shared_story --benchmark rendering_desktop
```

### 验证过程（改动 `wpr.py` 后怎么验证它没坏）

每次改完 `wpr.py`，按下面几步验证，不需要真的手动点开 Chrome：

1. **语法检查**（不用真的运行，只 parse AST）：
   ```bash
   python -c "import ast; ast.parse(open('wpr.py', encoding='utf-8').read()); print('OK')"
   ```

2. **看 `--help` 渲染是否正常**（新加的参数有没有正确出现在 usage/说明里）：
   ```bash
   python wpr.py --help
   ```

3. **单独测 story 解析逻辑**，不用起真正的 WPR/Chrome，直接调内部函数验证结果对不对：
   ```bash
   python -c "
   import sys; sys.path.insert(0, '.')
   from wpr import resolve_story_url, resolve_story_archive
   from pathlib import Path
   src = Path('chromium/src').resolve()
   print(resolve_story_url(src, 'microsoft_worker_fountains'))
   print(resolve_story_archive(src, 'microsoft_worker_fountains'))
   "
   ```
   预期输出应该能对上第 3 节文档里写的映射关系：
   ```
   ('http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html', WindowsPath('.../tough_canvas_cases.py'))
   (WindowsPath('.../rendering_desktop_004.wprgo'), WindowsPath('.../rendering_desktop.json'))
   ```

4. **测缺归档时的报错文案**（故意传一个不存在的归档路径）：
   ```bash
   python wpr.py --story microsoft_worker_fountains --archive nonexistent_test.wprgo
   ```
   应该直接报 `wprgo archive not found: ...`，而不是等到起 WPR 进程时才崩，也不是静默跳过。

5. **只测 `WprServer` 起停，不带 Chrome**（确认真的能拉起 wpr.exe、解析出端口、优雅关闭）：
   ```bash
   python -c "
   import sys; sys.path.insert(0, '.')
   from wpr import WprServer
   from pathlib import Path
   wpr_dir = Path('chromium/src/third_party/webpagereplay').resolve()
   archive = Path('chromium/src/tools/perf/page_sets/data/rendering_desktop_004.wprgo').resolve()
   s = WprServer(wpr_dir, archive)
   ports = s.start()
   print('PORTS:', ports)
   s.stop()
   print('STOPPED OK')
   "
   ```
   正常应该能看到类似：
   ```
   [wpr] Starting server on http://127.0.0.1:53088
   [wpr] Starting server on https://127.0.0.1:53089
   PORTS: {'http': 53088, 'https': 53089}
   STOPPED OK
   ```

以上 5 步都通过，才说明改动没有破坏原有功能；只有最后一步真正启动 Chrome 打开页面（完整跑一遍
模式 1 或模式 2 的命令）才是端到端确认。

### 例子 3：`ie_chalkboard`（HTTPS story，自定义 Chrome flags）

跟前面 `microsoft_worker_fountains` 是 HTTP 不同，这个 story 是 HTTPS，而且演示了怎么一次性传多个
自定义 Chrome flags：

```bash
python wpr.py \
  --args "--disable-skia-graphite --show-fps-counter --start-maximized --no-first-run" \
  --story=ie_chalkboard
```

解析结果：

```
[story] --story=ie_chalkboard -> --url=https://testdrive-archive.azurewebsites.net/performance/chalkboard/  (from .../tough_path_rendering_cases.py)
[story] --story=ie_chalkboard -> --archive=.../rendering_desktop_003.wprgo  (from .../rendering_desktop.json)
```

跑起来后终端会停在 `Press Enter here to stop Chrome and the WPR replay...`，这时候 Chrome 应该已经
最大化打开（`--start-maximized`），显示黑板背景的描边动画，左上角有 FPS 计数器
（`--show-fps-counter`）。用第 3 节"第 3 步"的 `gunzip | grep` 方法可以提前确认这个 URL 及其子资源
（CSS/字体/图片/JS/音频）确实在 `rendering_desktop_003.wprgo` 里，跑之前心里有底。

**如果页面打不开 / 是空白 / `ERR_EMPTY_RESPONSE`，按这个顺序排查：**

1. 看 Chrome 地址栏里实际打开的 URL，跟 `[story]` 打印出来的 `--url=` 是否完全一致（大小写、结尾
   斜杠、`http` 还是 `https` 都要一致）——不一致 WPR 会报 "no matching response"，页面加载失败。
2. 用上面"第 3 步"的方法确认这个 URL 真的在归档里，排除归档本身没录这个页面的可能。
3. 终端里 `[wpr] ... FAILED to find request ...` 是正常噪音（比如 Chrome 自带的
   `clients2.google.com/time/...` 时间同步请求），归档外的请求都会失败，这是预期行为，不代表
   目标页面加载失败。
4. `TLS handshake error ... first record does not look like a TLS handshake` 一般也可以忽略，是
   Chrome 往 HTTPS 代理端口发了非 TLS 流量（比如 QUIC 探测），不影响主页面。
5. 如果是 `ERR_EMPTY_RESPONSE`，很可能是第 8 节记录的 WPR 自带 bug（响应压缩格式协商失败），不是
   wpr.py 或 scheme 写错的问题——去看第 8 节。

想对比 Ganesh/Graphite，两个终端各跑一次，`--args` 换成 `--enable-skia-graphite` 版本，并各自指定
不同的 `--user-data-dir`（避免共用 profile 冲突）：

```bash
python wpr.py \
  --args "--enable-skia-graphite --show-fps-counter --start-maximized --no-first-run" \
  --story=ie_chalkboard \
  --user-data-dir C:\temp\wpr_profile_graphite
```

## 8. 已知问题：`ERR_EMPTY_RESPONSE`（WPR 自带的压缩协商 bug）

跑 `ie_chalkboard` 这个 story 时实际遇到过：Chrome 打开
`https://testdrive-archive.azurewebsites.net/performance/chalkboard/` 报
`ERR_EMPTY_RESPONSE`；加 `--disable-http2` 没用；把 URL 换成 `http://`（去掉 s）反而能打开。
这里记录排查过程和真正的根因，免得以后又去怀疑 scheme/http2。

### 排查过程

先排除了"是不是有另一个 Chrome SxS 实例把参数吃掉了"——`Get-Process chrome` 查出来的进程全是
`C:\Program Files\Google\Chrome\Application\`（正式版），跟 wpr.py 用的 Chrome SxS（Canary）是不同
二进制，不冲突。

然后绕开 Chrome，直接用 `curl` 打 WPR 的 http/https 端口做对照实验（起一个独立的 `WprServer`，
不经过 Chrome）：

- 不带 `Accept-Encoding` 头访问 https 端口 → WPR 报错、返回空的 `404`：
  ```
  [wpr] level=INFO msg="translating Content-Encoding" url=https://.../chalkboard/ origin=gzip client=""
  [wpr] level=ERROR msg="error recompressing response body" url=https://.../chalkboard/ error="unknown compression: "
  < HTTP/1.1 404 Not Found
  < Content-Length: 0
  ```
- 带上 `Accept-Encoding: gzip, deflate, br`（跟真实 Chrome 发的类似）访问同一个 https 端口 →
  正常返回 `200`：
  ```
  [wpr] level=INFO msg="Proxy: SERVING response" url=https://.../chalkboard/ status=200
  ```
- 把同样"不带 `Accept-Encoding`"的请求打到 **http** 端口（用代理方式，同一个归档）→ 报的是**一模
  一样**的错误（`unknown compression: `，`404`）。说明这个 bug 跟 http 还是 https 完全无关，
  只跟这一次请求实际携带的 `Accept-Encoding` 头有没有包含 `gzip` 有关。

### 根因（WPR 源码）

`third_party/webpagereplay/src/webpagereplay/proxy.go` 里，当归档存的 `Content-Encoding`
（这里是 `gzip`）跟请求的 `Accept-Encoding` 对不上时，WPR 会尝试解压再按客户端能接受的编码重新压缩：

```go
// proxy.go:143-166（节选）
clientAE := strings.ToLower(req.Header.Get("Accept-Encoding"))
originCE := strings.ToLower(storedResp.Header.Get("Content-Encoding"))
if !strings.Contains(clientAE, originCE) {
    body, err := ioutil.ReadAll(storedResp.Body)
    body, err = decompressBody(originCE, body)
    body, ce, err := CompressBody(clientAE, body)   // <-- 这里
    if err != nil {
        logger.Error("error recompressing response body", "error", err)
        w.WriteHeader(http.StatusNotFound)          // 直接吐空 404，不重试、不 fallback
        return
    }
    ...
}
```

`transformers.go:185-209` 的 `CompressBody(ae, ...)` 只认识 `gzip` / `deflate` / `br` 三种，
一旦 `ae`（即客户端实际发来的 `Accept-Encoding`）里三个都不包含（比如是空字符串），就直接返回
`"unknown compression: " + ae` 错误，上层直接回 `404` 空响应给客户端，**没有任何 fallback（比如
退化成不压缩直接发原文）**。这是 WPR 自带工具本身的一个粗糙之处，不是 `wpr.py` 脚本的 bug。

### 为什么现象上看起来像是"https 不行、http 行"

`.wprgo` 归档按 URL 做请求匹配时不是严格按 scheme 精确比对的（有内置的"模糊匹配"逻辑），所以哪怕
录制时这个 story 只有一份 `https://...chalkboard/` 的记录，用 `http://` 请求同一个 path 也可能被
匹配到同一份归档内容。真正决定成功还是 `ERR_EMPTY_RESPONSE` 的，是**那一次具体请求的
`Accept-Encoding` 头有没有被 WPR 正常识别出 `gzip`**——而不是 scheme 本身。也就是说，把 URL 换成
`http://` 之所以"能打开"，很可能只是巧合地绕开了触发这个 bug 的条件，不代表 https 天生有问题、
也不代表你看到的内容跟归档里录的完全一致（值得用浏览器里的 DevTools 核对一下页面内容和
`microsoft_worker_fountains` 那种能正常工作的例子做个对比）。

### 排查/验证方法（不用开 Chrome，直接对着 WPR 端口测）

```bash
python -c "
import sys; sys.path.insert(0, '.')
from wpr import WprServer
from pathlib import Path
wpr_dir = Path('chromium/src/third_party/webpagereplay').resolve()
archive = Path('chromium/src/tools/perf/page_sets/data/rendering_desktop_003.wprgo').resolve()
s = WprServer(wpr_dir, archive)
ports = s.start()
import subprocess
r = subprocess.run(['curl','-k','-sv','-H','Accept-Encoding: gzip, deflate, br',
                     '--connect-to','testdrive-archive.azurewebsites.net:443:127.0.0.1:%d' % ports['https'],
                     'https://testdrive-archive.azurewebsites.net/performance/chalkboard/'],
                    capture_output=True, text=True, timeout=20)
print(r.stderr[-500:])
s.stop()
"
```
看到 `Proxy: SERVING response ... status=200` 就说明这个特定的响应本身没问题，只是某些客户端请求
（比如缺 `Accept-Encoding` 或值不含 `gzip`）会触发这个 bug。

### workaround

暂时没有从 `wpr.py` 层面能干净修掉这个问题（bug 在 vendored 的 Go 代码里），实用的绕过方式：

- 就用已经验证能打开的 `http://testdrive-archive.azurewebsites.net/performance/chalkboard/`。
- 或者在 Chrome DevTools 的 Network 面板里看一下这次导航请求实际发出的 `Accept-Encoding` 头是什么
  （右键请求 → Headers），如果它确实不含 `gzip`，再进一步确认是不是有扩展程序/企业策略改写了这个
  头；一般情况下 Chrome 默认总会带 `gzip`，所以这种情况比较少见。
- 真要修，需要改 `third_party/webpagereplay/src/webpagereplay/transformers.go` 的
  `CompressBody`，给"未知/空 `Accept-Encoding`"加一个 identity（不压缩，直接发原文）的 fallback
  分支，而不是直接报错 404——这个改动不在本文档范围内，需要重新 `build.py` 编译 `wpr.exe`。

## 9. 已知问题：几乎所有 HTTPS story 都打不开（`ERR_EMPTY_RESPONSE` / "This page isn't working"）

实测 `ebay_pinch_2018`、`facebook_2018`、`gmail_2018` 这几个 story，用 `wpr.py --story=...`
打开真实 Chrome 后全部报 "This page isn't working"，现象和症状跟第 8 节的压缩协商 bug长得一样
（都是 `ERR_EMPTY_RESPONSE`），但根因完全不同、而且更基础、影响面更广——**任何 HTTPS story 都会
触发**，不是某个 story 或某次请求头凑巧触发的。加 `--disable-http2` 没用（之前怀疑过 HTTP/2
ALPN 协商的问题，这个假设是错的）。

### 根因

`wpr.py` 之前给 Chrome 传的参数是：

```
--proxy-server=http=127.0.0.1:<http_port>;https=127.0.0.1:<https_port>
```

Chrome 的 `--proxy-server` 语法里，`https=host:port` 只是告诉 Chrome"访问 `https://` 网址时把
请求转发到这个地址"，但**转发的方式永远是先发一个明文的 HTTP `CONNECT` 请求**，不管目标代理是
不是真的支持 HTTP 代理协议：

```
CONNECT www.ebay.com:443 HTTP/1.1
Host: www.ebay.com:443
...
```

这是用抓包（直接在 `https_port` 上开一个原始 socket 收字节）验证过的，Chrome 发的第一段字节就是
这行 `CONNECT`，不是 TLS ClientHello。

但 WPR 的 Go 实现（`third_party/webpagereplay/src/wpr.go:147` 的 flag 说明写的是 "Port number
to listen on for HTTP proxy requests **over an HTTPS connection**"）从设计上就是指望这个端口
**直接收到一个 TLS ClientHello**，完全没有实现 `CONNECT` 方法——在整个
`third_party/webpagereplay` 源码里 grep `CONNECT` / `MethodConnect` 是零匹配。于是 WPR 的 TLS
listener 拿着一段 `CONNECT www.ebay.com:443 HTTP/1.1\r\n...` 去做 TLS handshake，直接报
`tls: first record does not look like a TLS handshake`，连接被丢弃、不返回任何有效 HTTP
响应，Chrome 那边就表现成 `ERR_EMPTY_RESPONSE`。

反过来看 Telemetry/Catapult 自己跑 WPR 用的参数（`third_party/catapult/telemetry/telemetry/
internal/backends/chrome/chrome_startup_args.py`）根本不是 `http=`/`https=` 这种按 scheme 分流
的写法，而是：

```python
args.append('--proxy-server=socks://127.0.0.1:%s' % proxy_port)
args.append('--proxy-bypass-list=<-loopback>')
```

用 SOCKS 代理指向一个中间层 `ts_proxy_server`，由它按目标端口再转发给 WPR。也就是说 Chrome 从来
不会直接对着 WPR 的 https 端口发 `CONNECT`——官方工具链里本来就垫了一层。`wpr.py` 为了保持
简单（不想引入整套 `ts_proxy_server`/SOCKS 机制），改成自己实现一个轻量的 shim 补上这个缺口。

### 修复：`ConnectUnwrapProxy`

`wpr.py` 里新增了 `ConnectUnwrapProxy` 类：在本地再开一个端口，Chrome 的
`--proxy-server=https=...` 指向这个 shim 端口而不是 WPR 真正的 https 端口。shim 收到连接后：

1. 读到 `\r\n\r\n` 为止，识别出这是不是一个 `CONNECT` 请求；
2. 如果是，直接回一句 `HTTP/1.1 200 Connection Established\r\n\r\n`，把 `CONNECT` 那部分头
   吃掉、不转发；
3. 再单独跟 WPR 真正的 https 端口建一条新连接，把 Chrome 和 WPR 之间的原始字节双向直连转发
   （两个线程各自 `recv`/`sendall`，一端关闭时对面 `shutdown(SHUT_WR)`）；
4. 如果一开始的字节看起来不像 `CONNECT`（防御性分支，万一某个客户端真的直接发 TLS），就纯粹
   透明转发，不做任何处理。

`main()` 里的改动：

```python
ports = server.start()
# Chrome always speaks CONNECT to the "https=" proxy; WPR's https port
# expects a raw TLS ClientHello instead. See ConnectUnwrapProxy.
https_shim = ConnectUnwrapProxy(ports["https"])
https_shim.start()
...
chrome_cmd = [
    ...
    f'--proxy-server=http=127.0.0.1:{ports["http"]};https=127.0.0.1:{https_shim.port}',
    ...
]
...
finally:
    https_shim.stop()
    server.stop()
```

### 验证过程

用 headless Chrome 复现、对比修复前后（同一份归档 `rendering_desktop_f083c0f144.wprgo`，同一个
Chrome 二进制/参数，唯一变量是 https 代理端口有没有经过 shim）：

- 修复前：`--dump-dom http://www.ebay.com` 拿到 0 字节，stderr 里是
  `net::ERR_EMPTY_RESPONSE`。
- 修复后：同样的命令拿到 655901 字节的真实 eBay 首页 HTML，`<title>` 标签内容正确。

目前只针对 `ebay_pinch_2018` 做过这个端到端验证；但根因是协议层的、跟具体站点无关，理论上对
`facebook_2018`、`gmail_2018` 以及其它任何 HTTPS story 都同样适用。

### 和第 8 节 bug 的关系

两个 bug 表面症状完全一样（`ERR_EMPTY_RESPONSE`），但触发条件、根因、影响面都不同：

| | 第 8 节（压缩协商 bug） | 第 9 节（CONNECT/TLS 不匹配，本节） |
|---|---|---|
| 触发条件 | 某次具体请求的 `Accept-Encoding` 不含 `gzip`/`deflate`/`br` | 只要走 HTTPS，必然触发 |
| 影响面 | 偶发、跟具体请求头有关 | 所有 HTTPS story，系统性 |
| 根因位置 | `transformers.go` 的 `CompressBody` | Chrome 的 `CONNECT` vs WPR 的裸 TLS listener |
| 修复位置 | 无法在 `wpr.py` 层修，需改 vendored Go 代码 | 已在 `wpr.py` 里用 `ConnectUnwrapProxy` 修复 |

如果以后再遇到 `ERR_EMPTY_RESPONSE`，先确认 `wpr.py` 是不是用的是修复后的版本（有没有
`ConnectUnwrapProxy`），如果有且还报错，再去查是不是撞上了第 8 节这种 `Accept-Encoding` 边缘
情况。
