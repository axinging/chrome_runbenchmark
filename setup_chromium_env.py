"""Setup script to install tools and fetch minimal Chromium code on Windows.

This script prepares the environment to run graphite_vs_ganesh.py by:
1. Checking/installing Git
2. Cloning depot_tools
3. Configuring environment variables
4. Fetching Chromium source with --no-history (minimal)
5. Installing Python dependencies

Usage:
  python setup_chromium_env.py [--depot-tools-dir C:\\src\\depot_tools]
                               [--chromium-dir D:\\cr\\chromium]
                               [--skip-fetch]
                               [--skip-git-config]

Based on:
  https://chromium.googlesource.com/chromium/src/+/main/docs/windows_build_instructions.md
"""

import argparse
import json
import os
import subprocess
import sys
import winreg


# ============================================================
# Configuration
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'config.json')
DEFAULT_CHROMIUM_DIR = os.path.join(os.getcwd(), 'chromium')  # ./chromium in current directory

DEFAULT_DEPOT_TOOLS_DIR = r'C:\src\depot_tools'


def load_config():
    """Load local (untracked) config.json. Returns {} if missing."""
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f'Warning: cannot read {CONFIG_FILE}: {e}')
        return {}


CONFIG = load_config()
DEFAULT_PROXY = CONFIG.get('proxy', '')
DEPOT_TOOLS_REPO = 'https://chromium.googlesource.com/chromium/tools/depot_tools.git'

PYTHON_PACKAGES = ['html5lib', 'beautifulsoup4', 'six']


# ============================================================
# Utilities
# ============================================================

def setup_proxy(proxy):
    """Set proxy environment variables for the current process."""
    if not proxy:
        return
    print(f'[OK] Using proxy: {proxy}')
    os.environ['HTTP_PROXY'] = proxy
    os.environ['HTTPS_PROXY'] = proxy
    os.environ['http_proxy'] = proxy
    os.environ['https_proxy'] = proxy
    os.environ['no_proxy'] = 'localhost,127.0.0.1'
    os.environ['NO_PROXY'] = 'localhost,127.0.0.1'
    # For git
    subprocess.run(f'git config --global http.proxy {proxy}', shell=True,
                   capture_output=True)
    subprocess.run(f'git config --global https.proxy {proxy}', shell=True,
                   capture_output=True)


def run_cmd(cmd, cwd=None, check=True, shell=True):
    """Run a command and print it."""
    print(f'\n> {cmd}')
    result = subprocess.run(cmd, cwd=cwd, shell=shell, capture_output=False)
    if check and result.returncode != 0:
        print(f'[ERROR] Command failed with exit code {result.returncode}')
        sys.exit(1)
    return result


def cmd_exists(name):
    """Check if a command exists in PATH."""
    try:
        result = subprocess.run(
            f'where {name}', shell=True, capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False


def add_to_user_path(new_path):
    """Add a directory to the user's PATH environment variable (persistent)."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Environment',
            0,
            winreg.KEY_READ | winreg.KEY_WRITE
        )
        try:
            current_path, _ = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            current_path = ''

        # Check if already in PATH
        paths = [p.strip() for p in current_path.split(';') if p.strip()]
        if new_path.lower() not in [p.lower() for p in paths]:
            # Add to the front
            paths.insert(0, new_path)
            new_value = ';'.join(paths)
            winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, new_value)
            print(f'[OK] Added {new_path} to user PATH (front)')
        else:
            print(f'[OK] {new_path} already in user PATH')

        winreg.CloseKey(key)
    except Exception as e:
        print(f'[WARN] Could not modify registry PATH: {e}')
        print(f'       Please manually add {new_path} to the front of your PATH.')


def set_user_env_var(name, value):
    """Set a user environment variable (persistent)."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Environment',
            0,
            winreg.KEY_READ | winreg.KEY_WRITE
        )
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        print(f'[OK] Set user env var {name}={value}')
    except Exception as e:
        print(f'[WARN] Could not set env var {name}: {e}')
        print(f'       Please manually set {name}={value}')


def broadcast_env_change():
    """Notify Windows that environment variables have changed."""
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment',
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_long())
        )
    except Exception:
        pass


# ============================================================
# Step 1: Check Git
# ============================================================

def check_git():
    """Check if git is installed."""
    print('\n' + '=' * 60)
    print('Step 1: Checking Git installation')
    print('=' * 60)

    if cmd_exists('git'):
        result = subprocess.run(
            'git --version', shell=True, capture_output=True, text=True
        )
        print(f'[OK] Git found: {result.stdout.strip()}')
    else:
        print('[ERROR] Git is not installed.')
        print('Please download and install Git from: https://git-scm.com/download/win')
        print('After installing, re-run this script.')
        sys.exit(1)


# ============================================================
# Step 2: Clone depot_tools
# ============================================================

def _reset_depot_tools():
    """Reset depot_tools to clean state so gclient can self-update."""
    result = subprocess.run(
        'where gclient', shell=True, capture_output=True, text=True
    )
    if result.returncode == 0:
        dt_dir = os.path.dirname(result.stdout.strip().splitlines()[0])
        if os.path.isdir(os.path.join(dt_dir, '.git')):
            print(f'  Resetting depot_tools at {dt_dir} ...')
            subprocess.run(
                ['git', 'checkout', '.'], cwd=dt_dir,
                capture_output=True
            )


def setup_depot_tools(depot_tools_dir):
    """Clone depot_tools if not already present."""
    print('\n' + '=' * 60)
    print('Step 2: Setting up depot_tools')
    print('=' * 60)

    # First check if depot_tools is already available in PATH
    if cmd_exists('gclient'):
        result = subprocess.run(
            'where gclient', shell=True, capture_output=True, text=True
        )
        existing_path = os.path.dirname(result.stdout.strip().splitlines()[0])
        print(f'[OK] depot_tools already in PATH: {existing_path}')
        return
    elif os.path.exists(os.path.join(depot_tools_dir, 'gclient.py')):
        print(f'[OK] depot_tools already exists at {depot_tools_dir}')
    else:
        parent_dir = os.path.dirname(depot_tools_dir)
        os.makedirs(parent_dir, exist_ok=True)
        print(f'Cloning depot_tools to {depot_tools_dir}...')
        run_cmd(
            f'git clone {DEPOT_TOOLS_REPO} "{depot_tools_dir}"',
            cwd=parent_dir
        )
        print(f'[OK] depot_tools cloned to {depot_tools_dir}')

    # Add depot_tools to PATH for current session
    os.environ['PATH'] = depot_tools_dir + ';' + os.environ.get('PATH', '')

    # Persist to user PATH
    add_to_user_path(depot_tools_dir)

    # Set DEPOT_TOOLS_WIN_TOOLCHAIN=0
    os.environ['DEPOT_TOOLS_WIN_TOOLCHAIN'] = '0'
    set_user_env_var('DEPOT_TOOLS_WIN_TOOLCHAIN', '0')

    broadcast_env_change()

    # Run gclient once to bootstrap
    print('\nBootstrapping depot_tools (running gclient)...')
    run_cmd('gclient', check=False)


# ============================================================
# Step 3: Configure Git
# ============================================================

def configure_git(skip=False):
    """Configure git settings recommended for Chromium."""
    print('\n' + '=' * 60)
    print('Step 3: Configuring Git')
    print('=' * 60)

    if skip:
        print('[SKIP] Git configuration skipped by user request.')
        return

    git_configs = [
        ('core.autocrlf', 'false'),
        ('core.filemode', 'false'),
        ('core.preloadindex', 'true'),
        ('core.fscache', 'true'),
        ('core.longpaths', 'true'),
        ('branch.autosetuprebase', 'always'),
    ]

    for key, value in git_configs:
        run_cmd(f'git config --global {key} {value}')

    print('[OK] Git configured for Chromium development.')


# ============================================================
# Step 4: Fetch Chromium (--no-history, minimal deps)
# ============================================================

# Large deps to skip — not needed for running perf benchmarks.
# Only third_party/catapult is required (telemetry framework).
SKIP_DEPS = [
    'src/v8',
    'src/third_party/angle',
    'src/third_party/dawn',
    'src/third_party/skia',
    'src/third_party/swiftshader',
    'src/third_party/vulkan-deps',
    'src/third_party/vulkan-headers/src',
    'src/third_party/vulkan-loader/src',
    'src/third_party/vulkan-tools/src',
    'src/third_party/vulkan-utility-libraries/src',
    'src/third_party/vulkan-validation-layers/src',
    'src/third_party/vulkan_memory_allocator',
    'src/third_party/webrtc',
    'src/third_party/ffmpeg',
    'src/third_party/libaom/source/libaom',
    'src/third_party/libvpx/source/libvpx',
    'src/third_party/openh264/src',
    'src/third_party/pdfium',
    'src/third_party/devtools-frontend/src',
    'src/third_party/webgl/src',
    'src/third_party/llvm-build',
    'src/third_party/rust-toolchain',
    'src/third_party/node/linux',
    'src/third_party/node/mac',
    'src/third_party/node/mac_arm64',
    'src/third_party/node/win',
    'src/third_party/node/node_modules',
    'src/third_party/google_benchmark/src',
    'src/third_party/boringssl/src',
    'src/third_party/googletest/src',
    'src/third_party/nasm',
    'src/third_party/icu',
    'src/third_party/perfetto',
    'src/third_party/hunspell_dictionaries',
    'src/third_party/spirv-cross/src',
    'src/third_party/spirv-headers/src',
    'src/third_party/spirv-tools/src',
    'src/third_party/protobuf-javascript/src',
    'src/third_party/llvm-libc/src',
    'src/third_party/lss',
    # Android
    'src/third_party/android_build_tools',
    'src/third_party/android_deps',
    'src/third_party/android_platform',
    'src/third_party/android_sdk',
    'src/third_party/android_toolchain',
    'src/third_party/androidx',
    'src/third_party/android_media',
    'src/third_party/android_opengl',
    'src/third_party/android_prebuilts',
    'src/third_party/android_provider',
    'src/third_party/android_swipe_refresh',
    'src/third_party/android_system_sdk',
    'src/third_party/arcore-android-sdk',
    'src/third_party/arcore-android-sdk-client',
    # Graphics/GPU
    'src/third_party/webgpu-cts',
    'src/third_party/khronos',
    'src/third_party/glslang',
    'src/third_party/clang-format',
    # Media/codecs
    'src/third_party/dav1d',
    'src/third_party/opus',
    'src/third_party/flac',
    'src/third_party/libwebm',
    # ML/AI
    'src/third_party/tflite/src',
    'src/third_party/litert/src',
    'src/third_party/ml_dtypes',
    'src/third_party/sentencepiece',
    # Large libraries
    'src/third_party/grpc',
    'src/third_party/openscreen/src',
    'src/third_party/crashpad/crashpad',
    'src/third_party/breakpad/breakpad',
    'src/third_party/ink/src',
    'src/third_party/nearby/src',
    'src/third_party/fuchsia-sdk',
    'src/third_party/fuchsia-gn-sdk',
    'src/third_party/test_fonts',
    'src/third_party/jdk',
    'src/third_party/r8',
    'src/third_party/selenium-atoms',
    'src/third_party/siso',
    'src/third_party/content_analysis_sdk/src',
    'src/third_party/federated_compute/src',
    'src/third_party/cast_core/public/src',
    'src/third_party/cardboard/src',
    # Java/Kotlin
    'src/third_party/kotlin_stdlib',
    'src/third_party/kotlinc',
    'src/third_party/turbine',
    'src/third_party/byte_buddy',
    'src/third_party/mockito',
    'src/third_party/robolectric',
    'src/third_party/jacoco',
    'src/third_party/junit',
    'src/third_party/hamcrest',
    'src/third_party/google-truth',
    'src/third_party/google-java-format',
    'src/third_party/maven',
    # CIPD packages (platform-specific)
    'src/third_party/screen-ai/linux',
    'src/third_party/screen-ai/macos_amd64',
    'src/third_party/screen-ai/macos_arm64',
    'src/third_party/screen-ai/windows_amd64',
    'src/third_party/screen-ai/windows_386',
    'src/third_party/widevine/cdm/chromeos',
    'src/third_party/widevine/cdm/linux',
    'src/third_party/widevine/cdm/mac',
    'src/third_party/widevine/cdm/win',
    'src/third_party/widevine/scripts',
    # Other
    'src/third_party/webpagereplay',
    'src/third_party/perl',
    'src/third_party/sqlite/src',
    'src/third_party/crossbench',
    'src/third_party/crossbench-web-tests',
    'src/third_party/xnnpack/src',
    'src/third_party/apache-windows-arm64',
    'src/third_party/libphonenumber/src',
    'src/third_party/updater/chrome_linux64/cipd',
    'src/third_party/updater/chrome_mac_universal/cipd',
    'src/third_party/updater/chrome_mac_universal_prod/cipd',
    'src/third_party/updater/chrome_win_arm64/cipd',
    'src/third_party/updater/chrome_win_x86/cipd',
    'src/third_party/updater/chrome_win_x86_64/cipd',
    'src/third_party/updater/chrome_linux64_sans_iid/cipd',
    'src/third_party/updater/chrome_mac_universal_sans_iid/cipd',
    'src/third_party/updater/chrome_mac_universal_prod_sans_iid/cipd',
    'src/third_party/updater/chrome_win_arm64_sans_iid/cipd',
    'src/third_party/updater/chrome_win_x86_sans_iid/cipd',
    'src/third_party/updater/chrome_win_x86_64_sans_iid/cipd',
    'src/third_party/libc++/src',
    'src/third_party/libc++abi/src',
    'src/third_party/compiler-rt/src',
    'src/third_party/enterprise_companion/chromium_linux64/cipd',
    'src/third_party/enterprise_companion/chromium_mac_amd64/cipd',
    'src/third_party/enterprise_companion/chromium_mac_arm64/cipd',
    'src/third_party/enterprise_companion/chromium_win_x86/cipd',
    'src/third_party/enterprise_companion/chromium_win_x86_64/cipd',
    'src/third_party/crabbyavif/src',
    'src/third_party/webview2',
    'src/third_party/readability/src',
]

GCLIENT_TEMPLATE = '''solutions = [
  {{
    "name": "src",
    "url": "https://chromium.googlesource.com/chromium/src.git",
    "managed": False,
    "custom_deps": {{
{custom_deps}
    }},
    "custom_vars": {{
      "checkout_configuration": "small",
    }},
  }},
]
'''


def create_gclient_file(chromium_dir):
    """Create .gclient with custom_deps to skip large unnecessary repos."""
    gclient_file = os.path.join(chromium_dir, '.gclient')
    if os.path.exists(gclient_file):
        print(f'[OK] .gclient already exists at {gclient_file}')
        return

    custom_deps_lines = []
    for dep in SKIP_DEPS:
        custom_deps_lines.append(f'      "{dep}": None,')
    custom_deps = '\n'.join(custom_deps_lines)

    content = GCLIENT_TEMPLATE.format(custom_deps=custom_deps)

    with open(gclient_file, 'w') as f:
        f.write(content)
    print(f'[OK] Created {gclient_file} (skipping {len(SKIP_DEPS)} large deps)')


def fetch_chromium(chromium_dir):
    """Fetch Chromium source code with --no-history and minimal deps."""
    print('\n' + '=' * 60)
    print('Step 4: Fetching Chromium source (--no-history, minimal deps)')
    print('=' * 60)

    src_dir = os.path.join(chromium_dir, 'src')

    # Check if already fetched
    if os.path.exists(os.path.join(src_dir, '.git')):
        print(f'[OK] Chromium source already exists at {src_dir}')
        print('     Running gclient sync to update...')
        _reset_depot_tools()
        run_cmd('gclient sync -D --no-history', cwd=chromium_dir)
        return

    os.makedirs(chromium_dir, exist_ok=True)

    # Create .gclient with custom_deps (skip large repos)
    create_gclient_file(chromium_dir)

    # Clone main repo with --no-history
    if not os.path.exists(os.path.join(src_dir, '.git')):
        print('Cloning Chromium src (shallow, no history)...')
        run_cmd(
            'git clone --depth=1 https://chromium.googlesource.com/chromium/src.git src',
            cwd=chromium_dir
        )

    # Sync only the needed deps
    # Reset depot_tools to clean state so it can self-update to latest version
    _reset_depot_tools()
    print('Running gclient sync (skipping large deps)...')
    run_cmd('gclient sync -D --no-history --nohooks', cwd=chromium_dir)

    if os.path.exists(src_dir):
        print(f'[OK] Chromium source fetched to {src_dir}')
    else:
        print('[ERROR] Fetch completed but src/ directory not found.')
        sys.exit(1)


# ============================================================
# Step 5: Install Python dependencies
# ============================================================

def install_python_deps():
    """Install Python packages needed by graphite_vs_ganesh.py."""
    print('\n' + '=' * 60)
    print('Step 5: Installing Python dependencies')
    print('=' * 60)

    packages = ' '.join(PYTHON_PACKAGES)

    # Install with system python
    run_cmd(f'{sys.executable} -m pip install {packages}', check=False)

    # Try vpython3 if available
    if cmd_exists('vpython3'):
        run_cmd(f'vpython3 -m pip install {packages}', check=False)

    print('[OK] Python dependencies installed.')


# ============================================================
# Step 6: Verify setup
# ============================================================

def verify_setup(chromium_dir):
    """Verify that the environment is ready for graphite_vs_ganesh.py."""
    print('\n' + '=' * 60)
    print('Step 6: Verifying setup')
    print('=' * 60)

    src_dir = os.path.join(chromium_dir, 'src')
    checks = [
        ('depot_tools (gclient)', cmd_exists('gclient')),
        ('depot_tools (vpython3)', cmd_exists('vpython3')),
        ('tools/perf/run_benchmark', os.path.exists(
            os.path.join(src_dir, 'tools', 'perf', 'run_benchmark'))),
        ('third_party/catapult/telemetry', os.path.exists(
            os.path.join(src_dir, 'third_party', 'catapult', 'telemetry'))),
        ('graphiteperf/graphite_vs_ganesh.py', os.path.exists(
            os.path.join(src_dir, 'graphiteperf', 'graphite_vs_ganesh.py'))),
    ]

    all_ok = True
    for name, ok in checks:
        status = '[OK]' if ok else '[MISSING]'
        print(f'  {status} {name}')
        if not ok:
            all_ok = False

    if all_ok:
        print('\n[SUCCESS] Environment is ready!')
        print(f'\nTo run benchmarks:')
        print(f'  cd {src_dir}')
        print(f'  python graphiteperf\\graphite_vs_ganesh.py init')
        print(f'  python graphiteperf\\graphite_vs_ganesh.py run --story=<story_name>')
    else:
        print('\n[WARN] Some components are missing. Check the output above.')


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Setup Windows environment for Chromium perf benchmarks')
    parser.add_argument(
        '--depot-tools-dir', default=DEFAULT_DEPOT_TOOLS_DIR,
        help=f'Where to clone depot_tools (default: {DEFAULT_DEPOT_TOOLS_DIR})')
    parser.add_argument(
        '--chromium-dir', default=DEFAULT_CHROMIUM_DIR,
        help=f'Where to fetch Chromium (default: {DEFAULT_CHROMIUM_DIR})')
    parser.add_argument(
        '--skip-fetch', action='store_true',
        help='Skip fetching Chromium source code')
    parser.add_argument(
        '--skip-git-config', action='store_true',
        help='Skip git global configuration')
    parser.add_argument(
        '--proxy', default=DEFAULT_PROXY,
        help=f'HTTP/HTTPS proxy (default: "proxy" from config.json = '
             f'{DEFAULT_PROXY or "(none)"}). Use --proxy= to disable.')

    args = parser.parse_args()

    print('=' * 60)
    print('Chromium Environment Setup for graphite_vs_ganesh.py')
    print('=' * 60)
    print(f'  depot_tools dir: {args.depot_tools_dir}')
    print(f'  chromium dir:    {args.chromium_dir}')
    print(f'  proxy:           {args.proxy or "(none)"}')
    print()

    # Setup proxy before anything else
    setup_proxy(args.proxy)

    # Step 1: Check Git
    check_git()

    # Step 2: Clone and configure depot_tools
    setup_depot_tools(args.depot_tools_dir)

    # Step 3: Configure Git
    configure_git(skip=args.skip_git_config)

    # Step 4: Fetch Chromium
    if not args.skip_fetch:
        fetch_chromium(args.chromium_dir)
    else:
        print('\n[SKIP] Chromium fetch skipped by user request.')

    # Step 5: Install Python dependencies
    install_python_deps()

    # Step 6: Verify
    verify_setup(args.chromium_dir)


if __name__ == '__main__':
    main()
