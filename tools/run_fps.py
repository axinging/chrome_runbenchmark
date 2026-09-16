"""
run_benchmark_dc.py
Chrome CSS rendering benchmark - tests graphite-d3d11 with/without fps-counter and disable-direct-composition.
Requires: pip install psutil
"""

import argparse
import ctypes
import http.client
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime

# ==================== Dependencies ====================
try:
    import psutil
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil"])
    import psutil

try:
    import websocket as ws_module
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client"])
    import websocket as ws_module


# ==================== Helper Function ====================
def extract_command_info(command_line: str) -> str:
    """Extract config label from a Chrome command line."""
    # Part 0: Page/benchmark name
    if "css_opaque_divs" in command_line:
        part0 = "css"
    elif "webgl-animometer" in command_line or "Animometer" in command_line:
        part0 = "animometer"
    elif "aquarium" in command_line:
        part0 = "aquarium"
    elif "blob" in command_line:
        part0 = "blob"
    else:
        part0 = "unknown"

    # Part 1: Rendering backend
    if "enable-skia-graphite" in command_line and "--skia-graphite-backend=dawn-d3d12" in command_line:
        part1 = "graphite-d3d12"
    elif "enable-skia-graphite" in command_line and "--skia-graphite-backend=dawn-d3d11" in command_line:
        part1 = "graphite-d3d11"
    else:
        part1 = "ganesh"

    # Part 2: FPS counter
    part2 = "-fps" if "--show-fps-counter" in command_line else ""

    # Part 3: Layer count
    part3 = "-lc500" if "layer_count=500" in command_line else ""

    # Part 4: Direct composition disabled
    part4 = "-nodc" if "--disable-direct-composition" in command_line else ""

    return f"{part0}-{part1}{part2}{part3}{part4}"


def run_powershell_json(script: str):
    """Run a PowerShell script that emits JSON and return the parsed object."""
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                f"{script}"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    output = completed.stdout.strip()
    if not output:
        raise RuntimeError("PowerShell returned no JSON output.")
    return json.loads(output)


# ==================== GPU Usage via PDH (Performance Counters) ====================
from ctypes import wintypes

pdh = ctypes.windll.pdh

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("doubleValue", ctypes.c_double),
    ]


class GpuSampler:
    """Persistent PDH query for repeated GPU sampling without re-opening the query each time."""

    def __init__(self):
        self.hQuery = wintypes.HANDLE()
        self.hCounter = wintypes.HANDLE()
        self.available = False
        self._item_struct = None

        status = pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.hQuery))
        if status != 0:
            return

        counter_path = "\\GPU Engine(*)\\Utilization Percentage"
        status = pdh.PdhAddCounterW(self.hQuery, counter_path, 0, ctypes.byref(self.hCounter))
        if status != 0:
            pdh.PdhCloseQuery(self.hQuery)
            return

        # Initialize with first collect
        pdh.PdhCollectQueryData(self.hQuery)
        self.available = True

        class PDH_FMT_COUNTERVALUE_ITEM(ctypes.Structure):
            _fields_ = [
                ("szName", ctypes.c_wchar_p),
                ("FmtValue", PDH_FMT_COUNTERVALUE),
            ]

        self._item_struct = PDH_FMT_COUNTERVALUE_ITEM

    def sample(self) -> float:
        """Collect one sample. Call at ~1 sec intervals for accurate results."""
        if not self.available:
            return 0.0

        pdh.PdhCollectQueryData(self.hQuery)

        bufSize = wintypes.DWORD(0)
        itemCount = wintypes.DWORD(0)
        status = pdh.PdhGetFormattedCounterArrayW(
            self.hCounter, PDH_FMT_DOUBLE, ctypes.byref(bufSize), ctypes.byref(itemCount), None
        )

        if itemCount.value == 0:
            return 0.0

        buf = (ctypes.c_byte * bufSize.value)()
        status = pdh.PdhGetFormattedCounterArrayW(
            self.hCounter, PDH_FMT_DOUBLE, ctypes.byref(bufSize), ctypes.byref(itemCount),
            ctypes.cast(buf, ctypes.c_void_p)
        )
        if status != 0:
            return 0.0

        item_size = ctypes.sizeof(self._item_struct)
        engine_groups: dict[str, float] = {}
        for i in range(itemCount.value):
            item = self._item_struct.from_buffer_copy(buf, i * item_size)
            name = item.szName if item.szName else ""
            value = item.FmtValue.doubleValue if item.FmtValue.CStatus == 0 else 0.0
            m = re.search(r"phys_(\d+).*engtype_(.+)$", name)
            if m:
                key = f"phys_{m.group(1)}_{m.group(2)}"
                engine_groups[key] = engine_groups.get(key, 0.0) + value

        if not engine_groups:
            return 0.0
        return min(max(engine_groups.values()), 100.0)

    def close(self):
        if self.available:
            pdh.PdhCloseQuery(self.hQuery)
            self.available = False


# ==================== FPS Sampling via Chrome DevTools Protocol ====================
CDP_PORT = 9234


class FpsSampler:
    """Measure FPS via Chrome DevTools Protocol using requestAnimationFrame counting."""

    def __init__(self, port=CDP_PORT):
        self.port = port
        self.ws = None
        self.msg_id = 0
        self.available = False

    def connect(self, target_url=""):
        """Connect to Chrome DevTools and inject FPS counter script."""
        try:
            # Bypass proxy for localhost DevTools connections
            os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
            if "localhost" not in os.environ.get("NO_PROXY", ""):
                os.environ["NO_PROXY"] = os.environ["NO_PROXY"] + ",localhost,127.0.0.1"

            # Use http.client instead of urllib (Chrome 150+ rejects urllib's default headers)
            pages = None
            for attempt in range(5):
                try:
                    conn = http.client.HTTPConnection("localhost", self.port, timeout=5)
                    conn.request("GET", "/json", headers={"Host": f"localhost:{self.port}"})
                    resp = conn.getresponse()
                    if resp.status == 200:
                        pages = json.loads(resp.read().decode())
                        conn.close()
                        if pages:
                            break
                    else:
                        conn.close()
                except Exception:
                    pass
                time.sleep(2)

            if not pages:
                print("  FPS: No pages found in Chrome DevTools")
                return

            # Find the target page - prefer matching URL, otherwise first http(s) page
            target_page = None
            # Extract base URL for matching (before any query params)
            target_base = target_url.split("?")[0] if target_url else ""

            for page in pages:
                page_url = page.get("url", "")
                page_type = page.get("type", "")
                if page_type != "page":
                    continue
                if target_base and target_base in page_url:
                    target_page = page
                    break
                if page_url.startswith(("http://", "https://")) and not target_page:
                    target_page = page

            if not target_page:
                # Fallback to first page with a webSocketDebuggerUrl
                target_page = next((p for p in pages if p.get("webSocketDebuggerUrl")), None)

            if not target_page:
                print(f"  FPS: No suitable page found. Pages: {[p.get('url','')[:60] for p in pages]}")
                return

            ws_url = target_page.get("webSocketDebuggerUrl")
            if not ws_url:
                print(f"  FPS: No WebSocket URL for page: {target_page.get('url', '')[:60]}")
                return

            print(f"  FPS: Connecting to: {target_page.get('url', '')[:80]}")
            self.ws = ws_module.create_connection(ws_url, timeout=10)

            # First enable Runtime domain
            self._send("Runtime.enable")

            # Inject FPS counter script using requestAnimationFrame
            inject_script = (
                "(function() {"
                "  if (window.__fps_interval) clearInterval(window.__fps_interval);"
                "  window.__fps_count = 0;"
                "  window.__fps_value = 0;"
                "  var count = function() {"
                "    window.__fps_count++;"
                "    requestAnimationFrame(count);"
                "  };"
                "  requestAnimationFrame(count);"
                "  window.__fps_interval = setInterval(function() {"
                "    window.__fps_value = window.__fps_count;"
                "    window.__fps_count = 0;"
                "  }, 1000);"
                "})();"
            )
            result = self._send("Runtime.evaluate", {"expression": inject_script})
            if result.get("exceptionDetails"):
                print(f"  FPS: Script injection error: {result['exceptionDetails']}")
                return

            self.available = True
            # Wait for first measurement cycle to complete
            time.sleep(2)
            # Verify we can read a value
            test_val = self.sample()
            print(f"  FPS sampling: Connected (initial reading: {test_val:.0f})")
        except Exception as e:
            print(f"  FPS sampling: Failed to connect ({e})")
            self.available = False

    def _send(self, method, params=None):
        self.msg_id += 1
        msg = {"id": self.msg_id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        # Read response (skip events)
        while True:
            data = self.ws.recv()
            result = json.loads(data)
            if result.get("id") == self.msg_id:
                return result.get("result", {})

    def sample(self) -> float:
        """Get current FPS value."""
        if not self.available:
            return 0.0
        try:
            result = self._send("Runtime.evaluate", {
                "expression": "window.__fps_value",
                "returnByValue": True,
            })
            value = result.get("result", {}).get("value", 0)
            return float(value) if value else 0.0
        except Exception:
            return 0.0

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
            self.available = False


# ==================== System Information ====================
def get_system_info() -> dict:
    cpu_name = "Unknown CPU"
    gpu_name = "Unknown GPU"
    refresh_rate = "Unknown"

    try:
        system_data = run_powershell_json(
            ""
            "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name; "
            "$gpus = Get-CimInstance Win32_VideoController | Select-Object Name, CurrentRefreshRate; "
            "[pscustomobject]@{ cpu_name = $cpu; gpus = $gpus } | ConvertTo-Json -Compress -Depth 3"
        )

        cpu_name = str(system_data.get("cpu_name") or cpu_name).strip() or cpu_name

        gpu_list = system_data.get("gpus") or []
        if isinstance(gpu_list, dict):
            gpu_list = [gpu_list]

        if gpu_list:
            gpu_obj = next(
                (gpu for gpu in gpu_list if int(gpu.get("CurrentRefreshRate") or 0) > 0),
                gpu_list[0],
            )
            gpu_name = str(gpu_obj.get("Name") or gpu_name).strip() or gpu_name
            refresh_rate_value = gpu_obj.get("CurrentRefreshRate")
            if refresh_rate_value:
                refresh_rate = f"{refresh_rate_value}Hz"
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, RuntimeError):
        pass

    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    resolution = f"{width}x{height}"

    return {
        "cpu_name": cpu_name,
        "gpu_name": gpu_name,
        "resolution": resolution,
        "refresh_rate": refresh_rate,
    }


def sanitize_name(name: str) -> str:
    return re.sub(r"\s+", "_", re.sub(r'[\\/:*?"<>|@(),.\u00ae\u2122]', "", name)).strip("_")


# ==================== Kill Chrome ====================
def kill_benchmark_chrome(user_data_dir: str):
    """Kill only benchmark Chrome processes using the given profile directory."""
    killed = []
    normalized_dir = os.path.normcase(user_data_dir)

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            uses_profile = any(normalized_dir in os.path.normcase(arg) for arg in cmdline)
            if proc.info["name"] and proc.info["name"].lower() == "chrome.exe" and uses_profile:
                proc.kill()
                killed.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    for proc in killed:
        try:
            proc.wait(timeout=10)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass
    if killed:
        print(f"  Killed {len(killed)} Chrome process(es), waiting for cleanup...")
        time.sleep(3)
    else:
        time.sleep(1)


# ==================== Main ====================
def main():
    parser = argparse.ArgumentParser(description="Chrome rendering benchmark")
    parser.add_argument("--runs", type=int, default=1, help="Number of times to repeat the full benchmark (default: 1)")
    parser.add_argument("--fps", action="store_true", help="Enable FPS sampling via Chrome DevTools Protocol")
    parser.add_argument("--dryrun", action="store_true", help="Dry run: only test the first page")
    args = parser.parse_args()

    runs = args.runs
    measure_fps = args.fps
    dryrun = args.dryrun

    print("Collecting system information...")
    sys_info = get_system_info()

    cpu_name = sys_info["cpu_name"]
    gpu_name = sys_info["gpu_name"]
    resolution = sys_info["resolution"]
    refresh_rate = sys_info["refresh_rate"]

    cpu_safe = sanitize_name(cpu_name)
    gpu_safe = sanitize_name(gpu_name)

    print(f"  CPU        : {cpu_name}")
    print(f"  GPU        : {gpu_name}")
    print(f"  Resolution : {resolution}")
    print(f"  Refresh    : {refresh_rate}")
    print(f"  Runs       : {runs}")
    print()

    # Check GPU counter availability
    gpu_sampler = GpuSampler()
    if gpu_sampler.available:
        print("GPU performance counters: Available")
    else:
        print("GPU performance counters: NOT available (GPU usage will be 0)")
    print()

    # Chrome path
    user_name = os.environ["USERNAME"]
    chrome_path = rf"C:\Users\{user_name}\AppData\Local\Google\Chrome SxS\Application\chrome.exe"
    if not os.path.exists(chrome_path):
        print(f"ERROR: Chrome SxS not found at: {chrome_path}")
        print("Please install Chrome Canary or update the path.")
        sys.exit(1)
    print(f"Chrome SxS  : {chrome_path}")
    print()

    # ==================== Test URLs ====================
    test_urls = [
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html",
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html?use_attributes=1",
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html",
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&num_geometries=120000",
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&num_geometries=120000",
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&use_multi_draw=1&num_geometries=120000",
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&use_multi_draw=1&use_base_vertex_base_instance=1&num_geometries=120000",
        "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html?webgl_version=2&use_ubos=1&use_multi_draw=1",
        "http://webglsamples.org/aquarium/aquarium.html",
        "http://webglsamples.org/aquarium/aquarium.html?numFish=20000",
        "http://webglsamples.org/aquarium/aquarium.html?numFish=20000",
    ]
    if dryrun:
        test_urls = test_urls[:1]
        print("[DRYRUN] Only testing first page")

    # ==================== Test Commands ====================
    commands = []
    for url in test_urls:
        # graphite-d3d11 + fps
        commands.append(f"{url} --start-maximized --enable-experimental-web-platform-features --enable-skia-graphite --skia-graphite-backend=dawn-d3d11")
        # graphite-d3d11, no fps
        # ganesh only
        commands.append(f"{url} --start-maximized --enable-experimental-web-platform-features --disable-skia-graphite")

    script_dir = os.path.dirname(os.path.abspath(__file__))

    for run_num in range(1, runs + 1):
        if runs > 1:
            print()
            print("#" * 64)
            print(f"  RUN {run_num} / {runs}")
            print("#" * 64)
            print()

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder_name = f"usage-{cpu_safe}-{gpu_safe}-{resolution}-{refresh_rate}-{timestamp}"
        output_dir = os.path.join(script_dir, folder_name)
        os.makedirs(output_dir, exist_ok=True)
        print(f"  Output Dir : {output_dir}")

        all_results = []
        total = len(commands)

        for idx, cmd_args in enumerate(commands, 1):
            command_info = extract_command_info(f'"{chrome_path}" {cmd_args}')
            profile_dir = os.path.join(output_dir, f"chrome-profile-{idx:02d}-{command_info}")
            launch_args = [
                chrome_path,
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if measure_fps:
                launch_args.append(f"--remote-debugging-port={CDP_PORT}")
                launch_args.append("--remote-allow-origins=*")
            launch_args.extend(cmd_args.split())
            full_command = subprocess.list2cmdline(launch_args)
            command_info = extract_command_info(full_command)

            print("=" * 64)
            print(f" [{idx}/{total}] Config: {command_info}")
            print("=" * 64)

            # Start Chrome
            print("Starting Chrome...")
            subprocess.Popen(launch_args)

            # Wait 60 seconds
            print("Waiting 60 seconds for page to stabilize...")
            time.sleep(60)

            # Connect FPS sampler if enabled
            fps_sampler = None
            if measure_fps:
                # Extract the URL from cmd_args (first token)
                test_page_url = cmd_args.split()[0]
                fps_sampler = FpsSampler(port=CDP_PORT)
                fps_sampler.connect(target_url=test_page_url)

            # ---- Sampling (60 samples, ~1 sec each) ----
            samples = []
            print("Sampling for 60 seconds (1 sample/sec)...")

            # CPU baseline
            psutil.cpu_percent(interval=None)

            for i in range(1, 61):
                sample_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                time.sleep(1)

                cpu_usage = psutil.cpu_percent(interval=None)
                gpu_usage = gpu_sampler.sample() if gpu_sampler.available else 0.0
                fps_value = fps_sampler.sample() if fps_sampler and fps_sampler.available else 0.0

                sample_entry = {
                    "sample": i,
                    "time": sample_time,
                    "cpu": round(cpu_usage, 2),
                    "gpu": round(gpu_usage, 2),
                }
                if measure_fps:
                    sample_entry["fps"] = round(fps_value, 1)
                samples.append(sample_entry)

                if measure_fps:
                    print(f"  [{i:02d}/60] CPU: {cpu_usage:6.1f}%  GPU: {gpu_usage:6.1f}%  FPS: {fps_value:5.1f}")
                else:
                    print(f"  [{i:02d}/60] CPU: {cpu_usage:6.1f}%  GPU: {gpu_usage:6.1f}%")

            # Close FPS sampler
            if fps_sampler:
                fps_sampler.close()

            # Close benchmark Chrome
            print("Closing benchmark Chrome...")
            kill_benchmark_chrome(profile_dir)
            time.sleep(3)

            # ---- Statistics ----
            cpu_values = [s["cpu"] for s in samples]
            gpu_values = [s["gpu"] for s in samples]

            cpu_mean = statistics.mean(cpu_values)
            gpu_mean = statistics.mean(gpu_values)
            cpu_variance = statistics.pvariance(cpu_values)
            gpu_variance = statistics.pvariance(gpu_values)

            stats_dict = {
                "cpu_mean": round(cpu_mean, 2),
                "cpu_variance": round(cpu_variance, 2),
                "gpu_mean": round(gpu_mean, 2),
                "gpu_variance": round(gpu_variance, 2),
                "sample_count": len(samples),
            }

            if measure_fps:
                fps_values = [s["fps"] for s in samples]
                fps_mean = statistics.mean(fps_values)
                fps_variance = statistics.pvariance(fps_values)
                stats_dict["fps_mean"] = round(fps_mean, 1)
                stats_dict["fps_variance"] = round(fps_variance, 2)

            # ---- Save JSON ----
            json_data = {
                "config": command_info,
                "command": full_command,
                "system": {
                    "cpu": cpu_name,
                    "gpu": gpu_name,
                    "resolution": resolution,
                    "refresh_rate": refresh_rate,
                },
                "statistics": stats_dict,
                "samples": samples,
            }

            json_filename = f"usage-{command_info}-{cpu_safe}-{gpu_safe}-{resolution}-{refresh_rate}-{timestamp}.json"
            json_path = os.path.join(output_dir, json_filename)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)

            print(f"Saved: {json_filename}")
            print()

            all_results.append({
                "Config": command_info,
                "CPU_Mean(%)": round(cpu_mean, 2),
                "CPU_Var(%^2)": round(cpu_variance, 2),
                "GPU_Mean(%)": round(gpu_mean, 2),
                "GPU_Var(%^2)": round(gpu_variance, 2),
                **({"FPS_Mean": round(fps_mean, 1)} if measure_fps else {}),
                "samples": samples,
            })

        # ==================== Summary Table ====================
        print()
        print("=" * 64)
        print(f"                      SUMMARY TABLE (Run {run_num}/{runs})")
        print("=" * 64)
        print(f"CPU      : {cpu_name}")
        print(f"GPU      : {gpu_name}")
        print(f"Display  : {resolution} @ {refresh_rate}")
        print(f"Samples  : 60 per config (1/sec after 60s warm-up)")
        print()

        headers = ["Config", "CPU_Mean(%)", "CPU_Var(%^2)", "GPU_Mean(%)", "GPU_Var(%^2)"]
        if measure_fps:
            headers.append("FPS_Mean")
        col_widths = [max(len(h), max((len(str(r.get(h, ""))) for r in all_results), default=0)) for h in headers]

        header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
        sep_line = "-+-".join("-" * w for w in col_widths)
        print(header_line)
        print(sep_line)
        for r in all_results:
            row = " | ".join(str(r.get(h, "")).ljust(w) for h, w in zip(headers, col_widths))
            print(row)
        print()

        # Save summary CSV
        csv_path = os.path.join(output_dir, f"summary-{timestamp}.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(",".join(headers) + "\n")
            for r in all_results:
                f.write(",".join(str(r.get(h, "")) for h in headers) + "\n")

        # ---- Generate SVG charts for each config ----
        def make_svg_chart(config_name, samples_data, has_fps):
            """Generate an inline SVG line chart showing GPU% and optionally FPS over time."""
            width, height = 700, 200
            margin_l, margin_r, margin_t, margin_b = 50, 20, 30, 30
            plot_w = width - margin_l - margin_r
            plot_h = height - margin_t - margin_b
            n = len(samples_data)
            if n == 0:
                return ""

            gpu_vals = [s["gpu"] for s in samples_data]
            gpu_max = max(max(gpu_vals), 1)

            # GPU line points
            gpu_points = []
            for i, v in enumerate(gpu_vals):
                x = margin_l + (i / max(n - 1, 1)) * plot_w
                y = margin_t + plot_h - (v / gpu_max) * plot_h
                gpu_points.append(f"{x:.1f},{y:.1f}")
            gpu_polyline = " ".join(gpu_points)

            fps_polyline = ""
            fps_max = 1
            if has_fps:
                fps_vals = [s.get("fps", 0) for s in samples_data]
                fps_max = max(max(fps_vals), 1)
                fps_points = []
                for i, v in enumerate(fps_vals):
                    x = margin_l + (i / max(n - 1, 1)) * plot_w
                    y = margin_t + plot_h - (v / fps_max) * plot_h
                    fps_points.append(f"{x:.1f},{y:.1f}")
                fps_polyline = " ".join(fps_points)

            svg = f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  <rect width="{width}" height="{height}" fill="#fafafa" rx="4"/>
  <text x="{margin_l}" y="18" font-size="12" fill="#333">{config_name}</text>
  <!-- Grid lines -->
  <line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t + plot_h}" stroke="#ccc" stroke-width="1"/>
  <line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{margin_l + plot_w}" y2="{margin_t + plot_h}" stroke="#ccc" stroke-width="1"/>
  <!-- Y axis labels -->
  <text x="5" y="{margin_t + 4}" font-size="10" fill="#e74c3c">GPU {gpu_max:.0f}%</text>
  <text x="5" y="{margin_t + plot_h}" font-size="10" fill="#e74c3c">0%</text>
  <!-- GPU line -->
  <polyline points="{gpu_polyline}" fill="none" stroke="#e74c3c" stroke-width="1.5" opacity="0.8"/>'''

            if has_fps:
                svg += f'''
  <!-- FPS Y axis -->
  <text x="{margin_l + plot_w - 40}" y="{margin_t + 4}" font-size="10" fill="#2980b9">FPS {fps_max:.0f}</text>
  <!-- FPS line -->
  <polyline points="{fps_polyline}" fill="none" stroke="#2980b9" stroke-width="1.5" opacity="0.8"/>'''

            svg += f'''
  <!-- Legend -->
  <rect x="{margin_l + 10}" y="{margin_t + 5}" width="12" height="3" fill="#e74c3c"/>
  <text x="{margin_l + 25}" y="{margin_t + 10}" font-size="10" fill="#e74c3c">GPU%</text>'''
            if has_fps:
                svg += f'''
  <rect x="{margin_l + 70}" y="{margin_t + 5}" width="12" height="3" fill="#2980b9"/>
  <text x="{margin_l + 85}" y="{margin_t + 10}" font-size="10" fill="#2980b9">FPS</text>'''
            svg += "\n</svg>"
            return svg

        # Build chart HTML
        charts_html = ""
        for r in all_results:
            chart_svg = make_svg_chart(r["Config"], r["samples"], measure_fps)
            charts_html += f'<div class="chart">{chart_svg}</div>\n'

        # Save summary HTML
        html_path = os.path.join(output_dir, f"summary-{timestamp}.html")
        fps_th = "<th>FPS Mean</th>" if measure_fps else ""
        table_rows = ""
        for r in all_results:
            fps_td = f"<td>{r.get('FPS_Mean', '')}</td>" if measure_fps else ""
            table_rows += f"""        <tr>
            <td>{r['Config']}</td>
            <td>{r['CPU_Mean(%)']}</td>
            <td>{r['CPU_Var(%^2)']}</td>
            <td>{r['GPU_Mean(%)']}</td>
            <td>{r['GPU_Var(%^2)']}</td>
            {fps_td}
        </tr>\n"""

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Rendering Benchmark Results (Direct Composition Test)</title>
    <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 40px; background: #f5f5f5; }}
        h1 {{ color: #333; }}
        .info {{ background: #fff; padding: 16px 24px; border-radius: 8px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .info p {{ margin: 4px 0; color: #555; }}
        .info span {{ font-weight: bold; color: #222; }}
        table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        th {{ background: #4a90d9; color: #fff; padding: 12px 16px; text-align: left; }}
        td {{ padding: 10px 16px; border-bottom: 1px solid #eee; }}
        tr:hover {{ background: #f0f7ff; }}
        tr:last-child td {{ border-bottom: none; }}
        .chart {{ background: #fff; padding: 16px; margin: 12px 0; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .charts-section {{ margin-top: 32px; }}
        .charts-section h2 {{ color: #555; }}
        .timestamp {{ color: #999; font-size: 0.9em; margin-top: 16px; }}
    </style>
</head>
<body>
    <h1>Rendering Benchmark Results (Direct Composition Test)</h1>
    <div class="info">
        <p>CPU: <span>{cpu_name}</span></p>
        <p>GPU: <span>{gpu_name}</span></p>
        <p>Display: <span>{resolution} @ {refresh_rate}</span></p>
        <p>Samples: <span>60 per config (1/sec after 60s warm-up)</span></p>
        <p>Run: <span>{run_num} / {runs}</span></p>
    </div>
    <table>
        <thead>
            <tr>
                <th>Config</th>
                <th>CPU Mean (%)</th>
                <th>CPU Variance (%&sup2;)</th>
                <th>GPU Mean (%)</th>
                <th>GPU Variance (%&sup2;)</th>
                {fps_th}
            </tr>
        </thead>
        <tbody>
{table_rows}        </tbody>
    </table>
    <div class="charts-section">
        <h2>Sample Waveforms (GPU%{' & FPS' if measure_fps else ''})</h2>
{charts_html}
    </div>
    <p class="timestamp">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</body>
</html>"""

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        print(f"Summary CSV  : {csv_path}")
        print(f"Summary HTML : {html_path}")
        print(f"Output folder: {output_dir}")
        print()

    gpu_sampler.close()
    print("All benchmark runs complete!")


if __name__ == "__main__":
    main()
