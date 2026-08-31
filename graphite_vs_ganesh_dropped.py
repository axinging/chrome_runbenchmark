"""Compare Graphite vs Ganesh rendering performance on Intel devices.

Commands:
  init   - Collect all available stories to stories.json
  run    - Run benchmark stories (graphite, ganesh, or both)
  setup  - Install Python dependencies (html5lib, etc.)
"""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Auto-detect SRC_DIR: if script is inside an existing chromium/src checkout,
# use parent dir; otherwise assume chromium/src is a subdirectory.
_parent_dir = os.path.dirname(SCRIPT_DIR)
if os.path.exists(os.path.join(_parent_dir, 'tools', 'perf', 'run_benchmark')):
    DEFAULT_SRC_DIR = _parent_dir
else:
    DEFAULT_SRC_DIR = os.path.join(SCRIPT_DIR, 'chromium', 'src')

SRC_DIR = DEFAULT_SRC_DIR
STORIES_FILE = os.path.join(SCRIPT_DIR, 'stories.json')

DEFAULT_CHROME_DIR = os.path.join(
    os.environ['LOCALAPPDATA'], 'Google', 'Chrome SxS', 'Application'
)
BROWSER_EXECUTABLE = os.path.join(DEFAULT_CHROME_DIR, 'chrome.exe')
DEFAULT_PROXY = 'http://proxy..com:11'
PROXY = ''

# Ensure localhost connections (DevTools WebSocket) bypass the proxy
os.environ['no_proxy'] = 'localhost,127.0.0.1'
os.environ['NO_PROXY'] = 'localhost,127.0.0.1'
COMMON_FLAGS = (
    '--ignore-certificate-errors '
    '--ignore-ssl-errors '
    '--no-sandbox '
    '--allow-insecure-localhost '
    '--allow-running-insecure-content '
    '--ignore-urlfetcher-cert-requests '
    '--disable-web-security '
    '--disable-features=InsecureFormSubmissionInterstitial,HstsPreloading '
    '--test-type '
    #'--restore-last-session=false '
)
GRAPHITE_FLAGS = '--enable-skia-graphite --skia-graphite-backend=dawn-d3d11 ' + COMMON_FLAGS
GANESH_FLAGS = '--disable-skia-graphite ' + COMMON_FLAGS

SETUP_PACKAGES = ['html5lib', 'beautifulsoup4', 'six']


# ============================================================
# init command
# ============================================================

def init_stories():
    """Install dependencies and collect all available stories to stories.json."""
    setup_deps()
    cmd = [
        'vpython3', 'tools/perf/run_benchmark', 'list',
        'rendering.desktop', '--detailed',
        '--browser=exact',
        f'--browser-executable={BROWSER_EXECUTABLE}',
    ]
    print(f'Running: {" ".join(cmd)}')
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=SRC_DIR, shell=True
    )
    if result.returncode != 0:
        print(f'Error:\n{result.stderr}')
        sys.exit(1)

    # Parse story names from output
    # Format: "    Story N: {'name': 'story_name', 'tags': [...]}"
    stories = []
    for line in result.stdout.splitlines():
        m = re.search(r"'name':\s*'([^']+)'", line)
        if m:
            stories.append(m.group(1))

    with open(STORIES_FILE, 'w') as f:
        json.dump(stories, f, indent=2)

    print(f'Saved {len(stories)} stories to {STORIES_FILE}')
    print('Stories:', stories[:10], '...' if len(stories) > 10 else '')

    # Save raw output for reference
    raw_file = os.path.join(SCRIPT_DIR, 'stories_raw.txt')
    with open(raw_file, 'w') as f:
        f.write(result.stdout)
    print(f'Raw output saved to {raw_file}')


# ============================================================
# system info
# ============================================================

def _wmic_query(wmic_cmd):
    """Run a wmic command and return stripped output."""
    try:
        r = subprocess.run(wmic_cmd, shell=True, capture_output=True, text=True)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        # Skip header line
        return lines[1] if len(lines) > 1 else ''
    except Exception:
        return ''


_VENDOR_MAP = {'10de': 'NVIDIA', '8086': 'Intel', '1002': 'AMD',
               '13b5': 'ARM', '5143': 'Qualcomm', '106b': 'Apple'}


def get_active_gpu_from_chrome(extra_flags=''):
    """Launch headless Chrome on a WebGL probe page, return the active GPU info.

    Returns a dict with keys: vendor, device_id, driver, gl_renderer, raw.
    Empty strings if detection fails. This reflects the GPU that Chrome
    actually selected (which on hybrid NV+Intel machines may differ from the
    first adapter wmic enumerates, and may differ between Graphite/Ganesh).

    chrome://gpu can't be used in headless mode because the GPU info is
    fetched asynchronously from the browser process and isn't populated in
    the DOM before --dump-dom snapshots it. Instead we load a local HTML
    file that creates a WebGL context and reads the UNMASKED renderer/vendor
    via WEBGL_debug_renderer_info, which is what Chrome itself displays.
    Driver version isn't exposed to the page, so we fall back to wmic for it.
    """
    import tempfile
    empty = {'vendor': '', 'device_id': '', 'driver': '',
             'gl_renderer': '', 'raw': ''}
    if not os.path.isfile(BROWSER_EXECUTABLE):
        return empty
    # Only the user-installed Chrome Canary ("Chrome SxS") is probed for GPU
    # info via headless Chrome. Self-compiled builds can hang the headless
    # probe, so for them we skip it and just report the actual chrome path.
    if 'Chrome SxS' not in BROWSER_EXECUTABLE:
        return {'vendor': '', 'device_id': '', 'driver': '',
                'gl_renderer': BROWSER_EXECUTABLE, 'raw': ''}
    probe_html = (
        "<!doctype html><body><pre id=o></pre><script>\n"
        "const c=document.createElement('canvas');\n"
        "const gl=c.getContext('webgl2')||c.getContext('webgl');\n"
        "const ext=gl&&gl.getExtension('WEBGL_debug_renderer_info');\n"
        "document.getElementById('o').textContent='GPUINFO:'+JSON.stringify({\n"
        "  vendor: gl&&gl.getParameter(gl.VENDOR),\n"
        "  renderer: gl&&gl.getParameter(gl.RENDERER),\n"
        "  unmaskedVendor: ext&&gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),\n"
        "  unmaskedRenderer: ext&&gl.getParameter(ext.UNMASKED_RENDERER_WEBGL),\n"
        "})+':END';\n"
        "</script></body>"
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, 'probe.html')
            with open(probe, 'w', encoding='utf-8') as f:
                f.write(probe_html)
            url = 'file:///' + probe.replace('\\', '/')
            cmd = (
                f'"{BROWSER_EXECUTABLE}" --headless=new --no-sandbox '
                f'--disable-gpu-sandbox --user-data-dir="{tmp}\\ud" '
                f'--virtual-time-budget=5000 {extra_flags} '
                f'--dump-dom "{url}"'
            )
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=90)
        out = r.stdout or ''
    except Exception:
        return empty

    m = re.search(r'GPUINFO:(.*?):END', out, re.S)
    if not m:
        return empty
    try:
        data = json.loads(m.group(1))
    except Exception:
        return empty

    unmasked_renderer = (data.get('unmaskedRenderer') or '').strip()
    unmasked_vendor = (data.get('unmaskedVendor') or '').strip()
    # unmaskedVendor looks like "Google Inc. (Intel)" / "Google Inc. (NVIDIA)".
    vendor = ''
    vm = re.search(r'\(([^)]+)\)\s*$', unmasked_vendor)
    if vm:
        vendor = vm.group(1).strip()
    else:
        vendor = unmasked_vendor
    for key in _VENDOR_MAP.values():
        if key.lower() in unmasked_renderer.lower() or key.lower() in vendor.lower():
            vendor = key
            break

    # Pull the hex device id out of the ANGLE renderer string if present:
    #   "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x0000A780) Direct3D11 ...)"
    device_hex = ''
    dm = re.search(r'\(0x([0-9a-fA-F]+)\)', unmasked_renderer)
    if dm:
        device_hex = dm.group(1).lower()

    # Try to pick the matching driver version from wmic by vendor substring.
    driver = ''
    try:
        r2 = subprocess.run(
            'wmic path win32_videocontroller get name,driverversion /format:csv',
            shell=True, capture_output=True, text=True)
        for line in (r2.stdout or '').splitlines():
            line = line.strip()
            if not line or line.lower().startswith('node'):
                continue
            parts = [p.strip() for p in line.split(',')]
            # csv columns: Node, DriverVersion, Name
            if len(parts) >= 3 and vendor and vendor.lower() in parts[2].lower():
                driver = parts[1]
                break
    except Exception:
        pass

    return {
        'vendor': vendor,
        'device_id': device_hex,
        'driver': driver,
        'gl_renderer': unmasked_renderer,
        'raw': json.dumps(data),
    }


def get_system_info(use_chrome_gpu=False):
    """Collect GPU, CPU, display, OS, Chrome info.

    The headless Chrome GPU probe is off by default (it can be slow/unreliable
    on some builds); pass use_chrome_gpu=True to enable it. When disabled, GPU
    info comes entirely from wmic.
    """
    # Prefer Chrome's view of the active GPU (handles hybrid NV+Intel laptops
    # correctly); fall back to wmic when the probe is disabled or fails.
    if use_chrome_gpu:
        chrome_gpu = get_active_gpu_from_chrome()
    else:
        chrome_gpu = {'vendor': '', 'device_id': '', 'driver': '',
                      'gl_renderer': '', 'raw': ''}
    if chrome_gpu['gl_renderer'] or chrome_gpu['vendor']:
        gpu_name = chrome_gpu['gl_renderer'] or chrome_gpu['vendor']
        gpu_driver = chrome_gpu['driver'] or _wmic_query(
            'wmic path win32_videocontroller get driverversion')
    else:
        gpu_name = _wmic_query('wmic path win32_videocontroller get name')
        gpu_driver = _wmic_query(
            'wmic path win32_videocontroller get driverversion')
    cpu_name = _wmic_query('wmic cpu get name')

    # Display resolution
    try:
        r = subprocess.run(
            'wmic path win32_videocontroller get CurrentHorizontalResolution,CurrentVerticalResolution',
            shell=True, capture_output=True, text=True)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if len(lines) > 1:
            parts = lines[1].split()
            resolution = f'{parts[0]}x{parts[1]}' if len(parts) >= 2 else 'unknown'
        else:
            resolution = 'unknown'
    except Exception:
        resolution = 'unknown'

    # Display refresh rate
    refresh_rate = _wmic_query('wmic path win32_videocontroller get CurrentRefreshRate')
    if refresh_rate:
        refresh_rate = f'{refresh_rate}Hz'
    else:
        refresh_rate = 'unknown'

    # Windows version
    win_version = platform.platform()

    # Chrome version
    chrome_version = 'unknown'
    try:
        chrome_dir = os.path.dirname(BROWSER_EXECUTABLE)
        # Chrome version is the folder name containing chrome.dll
        for item in os.listdir(chrome_dir):
            if re.match(r'^\d+\.', item) and os.path.isdir(os.path.join(chrome_dir, item)):
                chrome_version = item
                break
    except Exception:
        pass

    return {
        'gpu_name': gpu_name,
        'gpu_driver': gpu_driver,
        'gpu_vendor': chrome_gpu['vendor'],
        'gpu_device_id': chrome_gpu['device_id'],
        'gpu_source': 'chrome' if chrome_gpu['gl_renderer'] or chrome_gpu['vendor'] else 'wmic',
        'gpu_raw': chrome_gpu['raw'],
        'resolution': resolution,
        'refresh_rate': refresh_rate,
        'cpu_name': cpu_name,
        'windows_version': win_version,
        'chrome_version': chrome_version,
    }


def _sanitize(s, max_len=30):
    """Sanitize string for use in folder name."""
    s = re.sub(r'[\\/:*?"<>|]', '', s)
    s = re.sub(r'\s+', '_', s.strip())
    return s[:max_len]


def _short_gpu_name(info, max_len=20):
    """Build a compact GPU label for use in folder names.

    Examples:
      ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 ...) -> RTX4090
      ANGLE (NVIDIA, NVIDIA GeForce RTX 5070 Direct3D11 ...) -> RTX5070
      Intel(R) Arc(TM) Graphics                              -> ArcGraphics
      Intel(R) UHD Graphics 770                              -> UHDGraphics770
      AMD Radeon RX 7900 XTX                                 -> RX7900XTX
    """
    name = info.get('gpu_name', '') or ''
    model = name
    m = re.search(r'ANGLE\s*\(([^,]+),\s*(.*)\)', model)
    if m:
        model = m.group(2)
    # Trim ANGLE/D3D backend suffixes.
    model = re.split(
        r'\s+(?:Direct3D\d*|D3D\d*|Vulkan|OpenGL|Metal|vs_\d|ps_\d)',
        model, maxsplit=1)[0]
    # Drop trademark noise, hex device IDs, and brand/vendor words.
    model = re.sub(r'\(R\)|\(TM\)|\(C\)', '', model)
    model = re.sub(r'\(0x[0-9a-fA-F]+\)', '', model)
    for word in ('NVIDIA', 'GeForce', 'Intel', 'AMD', 'Radeon',
                 'Corporation', 'Inc'):
        model = re.sub(rf'\b{word}\b', '', model, flags=re.IGNORECASE)
    # Collapse spaces/dashes so "RTX 4090" -> "RTX4090".
    model = re.sub(r'[\s\-_]+', '', model).strip(',')
    return _sanitize(model or 'GPU', max_len)


def _short_cpu_name(name, max_len=15):
    """Build a compact CPU label for use in folder names.

    Examples:
      Intel(R) Core(TM) Ultra 9 185H               -> 185H
      13th Gen Intel(R) Core(TM) i9-13900K         -> i9
      Intel(R) Core(TM) i7-12700K                  -> i7
      AMD Ryzen 9 7950X                            -> Ryzen7950X
    """
    if not name:
        return 'CPU'
    # Core Ultra: take the trailing model designator (e.g. "185H").
    m = re.search(r'Ultra\s+\d+\s+([A-Za-z0-9]+)', name)
    if m:
        return _sanitize(m.group(1), max_len)
    # Core iX: keep the tier only.
    m = re.search(r'\b(i[3579])\b', name)
    if m:
        return m.group(1)
    # Ryzen.
    m = re.search(r'Ryzen\s+\d+\s+([A-Za-z0-9]+)', name)
    if m:
        return _sanitize('Ryzen' + m.group(1), max_len)
    return _sanitize(name, max_len)


def get_results_dir(info, postfix=''):
    """Build results directory name; only timestamp (system info kept in info.txt)."""
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    folder_name = f'run-{ts}'
    if postfix:
        folder_name += f'-{_sanitize(postfix, 40)}'
    return os.path.join(SCRIPT_DIR, folder_name)


def write_info_txt(results_dir, info):
    """Write info.txt with system details into the results directory."""
    info_path = os.path.join(results_dir, 'info.txt')
    lines = [
        f'GPU: {info["gpu_name"]}',
        f'GPU Driver: {info["gpu_driver"]}',
        f'GPU Vendor: {info.get("gpu_vendor", "")}',
        f'GPU Device ID: {info.get("gpu_device_id", "")}',
        f'GPU Detection Source: {info.get("gpu_source", "wmic")}',
        f'Resolution: {info["resolution"]}',
        f'Refresh Rate: {info["refresh_rate"]}',
        f'CPU: {info["cpu_name"]}',
        f'Windows: {info["windows_version"]}',
        f'Chrome: {info["chrome_version"]}',
        f'Timestamp: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'Graphite flags: {GRAPHITE_FLAGS}',
        f'Ganesh flags: {GANESH_FLAGS}',
    ]
    with open(info_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'System info saved to {info_path}')


import shutil
from pathlib import Path

# Path configuration
PROFILE_BASE_DIR = 'c:/temp'


def chrome_hash(chrome_exe=None, length=8):
    """Return a short hex hash of a chrome.exe path.

    Used to build a PROFILE_DIR that is unique per distinct chrome.exe path:
    the same path always hashes to the same string, and different paths hash
    to (almost certainly) different strings. Paths are normalized so that
    case/slash differences on Windows don't produce a different hash.
    """
    if chrome_exe is None:
        chrome_exe = BROWSER_EXECUTABLE
    normalized = os.path.normcase(os.path.abspath(chrome_exe))
    return hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:length]


def get_profile_dir(chrome_exe=None):
    """Build the PROFILE_DIR for the given chrome.exe under PROFILE_BASE_DIR."""
    return os.path.join(PROFILE_BASE_DIR, f'profile_{chrome_hash(chrome_exe)}')


PROFILE_DIR = Path(get_profile_dir())
SESSION_DIR = PROFILE_DIR / 'Default' / 'Sessions'

def clear_sessions_content(dir_path):
    """
    Remove all files and subdirectories inside the given directory,
    but keep the directory itself intact.
    """
    # --- FIX: Convert string to Path object if necessary ---
    dir_path = Path(dir_path)
    # --------------------------------------------------------

    if not dir_path.exists():
        print(f"⚠️ Directory does not exist: {dir_path} – nothing to clear")
        return

    if not dir_path.is_dir():
        print(f"❌ Path is not a directory: {dir_path}")
        return

    items = list(dir_path.iterdir())
    if not items:
        print("✅ Directory is already empty")
        return

    for item in items:
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
                print(f"🗑️ Deleted file: {item.name}")
            elif item.is_dir():
                shutil.rmtree(item)
                print(f"🗑️ Deleted subdirectory: {item.name}")
        except PermissionError:
            print(f"❌ Permission denied for {item.name} – make sure Chrome is closed")
        except Exception as e:
            print(f"❌ Failed to delete {item.name}: {e}")

# ============================================================
# run command
# ============================================================

def run_benchmark(story, label, extra_chrome_flags, results_dir, timeout=300):
    """Run a single benchmark story with timeout."""
    output_dir = os.path.join(results_dir, f'{story}_{label}')
    os.makedirs(output_dir, exist_ok=True)
    # Profile dir is unique per chrome.exe path so different Chrome builds
    # don't share (or clobber) each other's profile.
    PROFILE_DIR = get_profile_dir(BROWSER_EXECUTABLE)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    #SESSION_DIR = PROFILE_DIR + '/Default/Sessions'
    SESSION_DIR = os.path.join(PROFILE_DIR, 'Default', 'Sessions')
    clear_sessions_content(SESSION_DIR)

    if PROXY:
        extra_args = (
            f'--proxy-server={PROXY} '
            f'--proxy-bypass-list=localhost;127.0.0.1;<local> '
            f'{extra_chrome_flags}'
        )
    else:
        extra_args = extra_chrome_flags
    cmd = (
        f'vpython3 tools/perf/run_benchmark run rendering.desktop '
        f'--browser=exact '
        f'--profile-dir={PROFILE_DIR} '
        f'--profile-type=exact '
        f'--browser-executable="{BROWSER_EXECUTABLE}" '
        f'--story={story} '
        f'--use-live-sites '
        f"--extra-browser-args=\"{extra_args}\" "
        f'--legacy-json-trace-format '
        f'--results-label={label} '
        f'--output-dir={output_dir}'
    )
    print(f'\n{"="*60}')
    print(f'[{label}] Running story: {story} (timeout={timeout}s)')
    print(f'{"="*60}')
    print(f'Command: {cmd}\n')

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, cwd=SRC_DIR, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=timeout
        )
        elapsed = time.time() - t0
        sys.stdout.write(result.stdout)
        print(f'[{label}/{story}] Duration: {elapsed:.1f}s')
        return result.returncode
    except subprocess.TimeoutExpired as e:
        elapsed = time.time() - t0
        if e.stdout:
            sys.stdout.write(e.stdout if isinstance(e.stdout, str)
                            else e.stdout.decode('utf-8', errors='replace'))
        print(f'\n[TIMEOUT] {label}/{story} killed after {elapsed:.1f}s (limit={timeout}s)')
        return -1


def run_stories(stories, mode='both', timeout=300, postfix=''):
    """Run stories for graphite, ganesh, or both."""
    # Collect system info and create results directory
    info = get_system_info()
    results_dir = get_results_dir(info, postfix)
    os.makedirs(results_dir, exist_ok=True)
    write_info_txt(results_dir, info)
    print(f'Results will be saved to: {results_dir}')
    print(f'Per-story timeout: {timeout}s\n')

    # Tee all output to a single log file in results directory
    log_file = os.path.join(results_dir, f'run_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
    _log_fh = open(log_file, 'w', encoding='utf-8')
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr

    class Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, data):
            for s in self.streams:
                s.write(data)
                s.flush()
        def flush(self):
            for s in self.streams:
                s.flush()

    sys.stdout = Tee(_orig_stdout, _log_fh)
    sys.stderr = Tee(_orig_stderr, _log_fh)

    results = {'graphite': {}, 'ganesh': {}}

    total_start = time.time()
    for story in stories:
        if mode in ('both', 'graphite'):
            rc = run_benchmark(story, 'graphite', GRAPHITE_FLAGS, results_dir, timeout)
            if rc == -1:
                results['graphite'][story] = 'TIMEOUT'
            else:
                results['graphite'][story] = 'PASS' if rc == 0 else 'FAIL'

        if mode in ('both', 'ganesh'):
            rc = run_benchmark(story, 'ganesh', GANESH_FLAGS, results_dir, timeout)
            if rc == -1:
                results['ganesh'][story] = 'TIMEOUT'
            else:
                results['ganesh'][story] = 'PASS' if rc == 0 else 'FAIL'

    # Save results summary
    results_file = os.path.join(results_dir, 'run_results.json')
    with open(results_file, 'w') as f:
        json.dump(results, f, indent=2)
    print(f'\nResults summary saved to {results_file}')

    # Print summary
    total_elapsed = time.time() - total_start
    print(f'\n{"="*60}')
    print('SUMMARY')
    print(f'{"="*60}')
    for backend, story_results in results.items():
        if story_results:
            passed = sum(1 for v in story_results.values() if v == 'PASS')
            failed = sum(1 for v in story_results.values() if v == 'FAIL')
            timed_out = sum(1 for v in story_results.values() if v == 'TIMEOUT')
            total = len(story_results)
            print(f'  {backend}: {passed}/{total} passed, {failed} failed, {timed_out} timeout')
    print(f'  Total time: {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)')

    # Restore stdout/stderr and close log
    sys.stdout = _orig_stdout
    sys.stderr = _orig_stderr
    _log_fh.close()
    print(f'Full log saved to: {log_file}')


# ============================================================
# patch command
# ============================================================

def patch_catapult():
    """Apply patches to catapult telemetry for proxy bypass."""
    catapult_dir = os.path.join(SRC_DIR, 'third_party', 'catapult')
    diff_file = os.path.join(SCRIPT_DIR, 'edit.diff')

    if not os.path.isdir(catapult_dir):
        print(f'Error: catapult directory not found: {catapult_dir}')
        sys.exit(1)
    if not os.path.isfile(diff_file):
        print(f'Error: patch file not found: {diff_file}')
        sys.exit(1)

    # Apply edit.diff to catapult (chrome_startup_args.py)
    print(f'Applying {diff_file} to {catapult_dir} ...')
    result = subprocess.run(
        ['git', 'apply', '--check', diff_file],
        cwd=catapult_dir, capture_output=True, text=True
    )
    if result.returncode == 0:
        subprocess.run(['git', 'apply', diff_file], cwd=catapult_dir)
        print('  chrome_startup_args.py patched.')
    else:
        print('  chrome_startup_args.py patch already applied or conflicts, skipping.')

    # Patch websocket wrapper to bypass proxy for localhost DevTools connections
    ws_file = os.path.join(
        catapult_dir, 'telemetry', 'telemetry', 'internal', 'backends',
        'chrome_inspector', 'websocket.py')
    if os.path.isfile(ws_file):
        with open(ws_file, 'r') as f:
            content = f.read()
        if 'http_no_proxy' not in content:
            old = "kwargs['sockopt'] = sockopt\n  return _create_connection(*args, **kwargs)"
            new = ("kwargs['sockopt'] = sockopt\n\n"
                   "  # Ensure DevTools WebSocket connections to localhost bypass any HTTP proxy.\n"
                   "  if 'http_no_proxy' not in kwargs:\n"
                   "    kwargs['http_no_proxy'] = ['localhost', '127.0.0.1']\n\n"
                   "  return _create_connection(*args, **kwargs)")
            patched = content.replace(old, new)
            if patched != content:
                with open(ws_file, 'w') as f:
                    f.write(patched)
                print('  websocket.py patched (no_proxy for localhost).')
            else:
                print('  websocket.py: pattern not matched, manual edit may be needed.')
        else:
            print('  websocket.py already patched.')
    else:
        print(f'  Warning: {ws_file} not found.')

    print('Patch complete.')


# ============================================================
# setup command
# ============================================================

def setup_deps():
    """Install Python dependencies required by the benchmark runner."""
    print('Installing Python dependencies...')
    for python in [sys.executable, 'vpython3']:
        cmd = [python, '-m', 'pip', 'install'] + SETUP_PACKAGES
        print(f'\nRunning: {" ".join(cmd)}')
        subprocess.run(cmd, shell=True)
    print('\nDone.')


# ============================================================
# main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Compare Graphite vs Ganesh rendering performance')
    parser.add_argument('--chromium-src', default=DEFAULT_SRC_DIR,
                        help=f'Path to chromium/src (default: {DEFAULT_SRC_DIR})')
    parser.add_argument('--chrome-dir', default=DEFAULT_CHROME_DIR,
                        help=f'Directory containing chrome.exe '
                             f'(default: {DEFAULT_CHROME_DIR})')
    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('init', help='Install deps and collect stories to stories.json')
    subparsers.add_parser('patch', help='Apply proxy bypass patches to catapult')

    run_parser = subparsers.add_parser('run', help='Run benchmark stories')
    run_parser.add_argument('--story', help='Run a specific story (default: all)')
    run_parser.add_argument('--mode', choices=['both', 'graphite', 'ganesh'],
                           default='both',
                           help='Which backend to test (default: both)')
    run_parser.add_argument('--timeout', type=int, default=1800,
                           help='Per-story timeout in seconds (default: 1800)')
    run_parser.add_argument('--no-proxy', action='store_true',
                           help='Disable proxy for Chrome')
    run_parser.add_argument('--proxy', default=None,
                           help='Override proxy URL (default: Intel proxy)')
    run_parser.add_argument('--postfix', default='',
                           help='Append a suffix to the results folder name')

    args = parser.parse_args()

    # Update global SRC_DIR based on argument
    global SRC_DIR
    SRC_DIR = args.chromium_src

    # Update global BROWSER_EXECUTABLE based on --chrome-dir argument
    global BROWSER_EXECUTABLE
    BROWSER_EXECUTABLE = os.path.join(args.chrome_dir, 'chrome.exe')

    if args.command == 'patch':
        patch_catapult()
    elif args.command == 'init':
        init_stories()
    elif args.command == 'run':
        global PROXY
        if args.no_proxy:
            PROXY = ''
        elif args.proxy:
            PROXY = args.proxy
        if args.story:
            stories = [args.story]
        else:
            if not os.path.exists(STORIES_FILE):
                print(f'Error: {STORIES_FILE} not found. Run "init" first.')
                sys.exit(1)
            with open(STORIES_FILE) as f:
                stories = json.load(f)
            print(f'Loaded {len(stories)} stories from {STORIES_FILE}')
        run_stories(stories, args.mode, args.timeout, args.postfix)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
