#!/usr/bin/env python3
r"""
Attach cdb.exe (console front-end of WinDbg) to Chrome's GPU process, set a
one-shot breakpoint on a driver-side function, and capture the call stack
(`kb`) once it's hit -- without leaving Chrome broken (detaches via `qd`).

Typical call chain this is meant to capture:
    chrome!...OMSetRenderTargets (or similar D3D11 call site)
      -> GPU Driver UMD (igd10iumd64.dll)
      -> d3d11!NDXGI::CDevice::WaitForSynchronizationObjectFromCpuCB

Note on symbols: `NDXGI::CDevice` is Microsoft's own DXGI runtime class
(lives in d3d11.dll in this build, not dxgi.dll) -- it is NOT an driver
symbol, so it resolves from Microsoft's public symbol server, never from
the driver PDB.

--------------------------------------------------------------------------
Two attach modes
--------------------------------------------------------------------------
1. Auto mode (default): the script launches chrome.exe itself with
   --extra-arg flags, polls `Get-CimInstance Win32_Process` for the child
   process whose command line contains `--type=gpu-process`, then attaches.
2. Manual mode: pass --pid <gpu_pid> for a Chrome instance you already
   started yourself (e.g. read the PID off a --gpu-startup-dialog box).
   The script skips launching Chrome entirely and just attaches.

--------------------------------------------------------------------------
Symbol sources (Chrome PDB and driver PDB are independent choices)
--------------------------------------------------------------------------
Chrome symbols:
    --chrome-pdb <dir>          use a local PDB directory (fastest, no
                                 network needed once you have the PDBs for
                                 this exact build).
    (default, no --chrome-pdb)  pull from the public Chromium symbol
                                 server via --chrome-symbol-server-url
                                 (default: chromium-browser-symsrv), cached
                                 locally under --chrome-symbol-cache-dir.

Driver symbols:
    --driver-pdb <dir>           use a local PDB directory instead of the
                                  symbol server.
    (default, no --driver-pdb)   pull from the driver internal symbol
                                  server via --driver-symbol-server-url
                                  (default:
                                  http://symbols.driver.com/symbols),
                                  cached under --driver-symbol-cache-dir.
                                  Requires the machine running cdb to be
                                  able to reach that internal server
                                  (domain-joined + correct WinHTTP proxy --
                                  see notes below if igd10iumd64.dll
                                  symbols show as FAILED even though
                                  dxgi.dll/d3d11.dll symbols succeed).

Windows system DLLs (dxgi.dll, d3d11.dll, ntdll.dll, ...) always resolve
from Microsoft's public symbol server (msdl.microsoft.com), cached under
--ms-symbol-cache-dir -- this is unconditional and independent of the
Chrome/driver choices above.

--------------------------------------------------------------------------
Prefetching symbols (--prefetch, default ON)
--------------------------------------------------------------------------
Before attaching, the script runs symchk.exe (ships next to windbg.exe)
against --prefetch-dxgi-dll, each --prefetch-chrome-file, and each
--prefetch-driver-dll (repeatable -- pass it once per file) to download
their symbols into the same cache directories used later by cdb. This
avoids the live cdb session stalling on network round-trips (or timing
out on --break-timeout) while a whole batch of symbols downloads for the
first time. Use --no-prefetch to skip this and let cdb download on demand
instead.

--------------------------------------------------------------------------
Why targeted `.reload /f <module>` instead of blanket `.reload /f`
--------------------------------------------------------------------------
A blanket `.reload /f` tries to resolve symbols for every one of the ~300
modules chrome.exe loads, round-tripping to the symbol server(s) for each
one -- this is what caused early timeouts. --reload-module (repeatable,
default: DEFAULT_RELOAD_MODULES below) restricts reload to just the
modules that matter for this call chain.

--------------------------------------------------------------------------
Examples
--------------------------------------------------------------------------
Simplest (auto mode, all defaults: driver symbols from GDHM server, Chrome
symbols from the Chromium symbol server, prefetch dxgi.dll only):
    python capture_driver_stack.py

Prefetch the actual driver DLL(s) too (recommended -- pass the real path(s)
under DriverStore\FileRepository on the target machine, repeatable):
    python capture_driver_stack.py ^
        --prefetch-driver-dll "C:\Windows\System32\DriverStore\FileRepository\iigd_dch.inf_amd64_XXXX\igd10iumd64.dll"

Also prefetch chrome.exe/chrome.dll from the Chromium symbol server (only
useful if this build's symbols are actually published there -- verify
first with check_driver_symbols.py against --chrome-symbol-server-url):
    python capture_driver_stack.py ^
        --prefetch-chrome-file "C:\debugdrivers\chrome.packed\chrome\Chrome-bin\chrome.exe" ^
        --prefetch-chrome-file "C:\debugdrivers\chrome.packed\chrome\Chrome-bin\154.0.8037.58\chrome.dll"

Use local PDBs for both Chrome and the driver instead of any symbol server:
    python capture_driver_stack.py ^
        --chrome-pdb D:\drivers\chrome-pdb ^
        --driver-pdb D:\drivers\DriverSymbols-Release-64-bit\pdb

Manual mode (PID already known, e.g. from --gpu-startup-dialog):
    python capture_driver_stack.py --pid 12345
"""


import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time

#DEFAULT_CHROME_EXE = r"C:\debugdrivers\chrome.packed\chrome\Chrome-bin\chrome.exe"
DEFAULT_CHROME_EXE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")),
    "Google", "Chrome SxS", "Application", "chrome.exe",
)
DEFAULT_URL = (
    "https://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/"
    "webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&use_multi_draw=1&num_geometries=120000"
)
DEFAULT_CHROME_SYMBOL_SERVER = "https://chromium-browser-symsrv.commondatastorage.googleapis.com"
DEFAULT_CHROME_SYMBOL_CACHE_DIR = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "chrome_symcache")
DEFAULT_DRIVER_SYMBOL_SERVER = "http://symbols.driver.com/symbols"
DEFAULT_DRIVER_SYMBOL_CACHE_DIR = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "gdhm_symcache")
DEFAULT_MS_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"
DEFAULT_MS_SYMBOL_CACHE_DIR = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "ms_symcache")
#DEFAULT_WINDBG = r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\windbg.exe"
DEFAULT_WINDBG = r"C:\Program Files\Windows Kits\10\Debuggers\x64\windbg.exe"
DEFAULT_BREAK_SYMBOL = "d3d11!NDXGI::CDevice::WaitForSynchronizationObjectFromCpuCB"
DEFAULT_OUTPUT_DIR = r".\data"
# Only these modules matter for the chrome -> dxgi -> UMD chain; targeted
# `.reload /f <module>` on just these avoids symbol-server round trips for the
# other ~300 modules chrome.exe loads, which is what caused earlier timeouts.
DEFAULT_RELOAD_MODULES = ["chrome.exe", "chrome.dll", "d3d11.dll", "dxgi.dll", "igd10iumd64.dll"]
DEFAULT_PREFETCH_DXGI_DLL = r"C:\Windows\System32\dxgi.dll"


def find_cdb(windbg_path: str) -> str:
    candidate = os.path.join(os.path.dirname(windbg_path), "cdb.exe")
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError(
        f"cdb.exe not found next to windbg.exe: {candidate}; pass --cdb to specify it manually"
    )


def find_symchk(windbg_path: str) -> str:
    """symchk.exe usually ships next to windbg.exe in the same Debuggers directory; used to prefetch/warm the local symbol cache so cdb doesn't hang the live process downloading symbols."""
    candidate = os.path.join(os.path.dirname(windbg_path), "symchk.exe")
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError(
        f"symchk.exe not found next to windbg.exe: {candidate}; prefetching needs it, pass --symchk to specify it manually"
    )


def prefetch_symbols(symchk_path: str, file_path: str, symbol_server: str, cache_dir: str) -> None:
    """Download a single file's symbols from the symbol server into the local cache ahead of time, so cdb's .reload later hits the local directory directly."""
    if not os.path.isfile(file_path):
        print(f"[!] Prefetch skipped, file not found: {file_path}", file=sys.stderr)
        return
    os.makedirs(cache_dir, exist_ok=True)
    sympath = f"SRV*{cache_dir}*{symbol_server}"
    cmd = [symchk_path, "/v", "/s", sympath, file_path]
    print("[prefetch]", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    passed = result.returncode == 0 and "FAILED files = 0" in result.stdout
    print(f"[prefetch] {'OK' if passed else 'FAILED'}: {os.path.basename(file_path)}")


def launch_chrome(chrome_exe: str, url: str, extra_args: list) -> subprocess.Popen:
    if not os.path.isfile(chrome_exe):
        raise FileNotFoundError(f"chrome.exe not found: {chrome_exe}")
    cmd = [chrome_exe] + extra_args + [url]
    print("[launch]", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return subprocess.Popen(cmd)


def _query_chrome_processes() -> list:
    """Return list of {ProcessId, ParentProcessId, CommandLine} for all chrome.exe."""
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
        "| Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return []
    data = json.loads(result.stdout)
    if isinstance(data, dict):
        data = [data]
    return data


def find_gpu_pid(timeout: float, poll_interval: float = 1.0) -> int:
    print(f"[*] Waiting up to {timeout:.0f}s, polling for the GPU process (--type=gpu-process) ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for proc in _query_chrome_processes():
            cmdline = proc.get("CommandLine") or ""
            if "--type=gpu-process" in cmdline:
                pid = proc["ProcessId"]
                print(f"[*] Found GPU process pid={pid}")
                return pid
        time.sleep(poll_interval)
    raise TimeoutError("Timed out waiting for the GPU process; check that Chrome started correctly, or specify --pid manually")


def build_cdb_script(
    chrome_pdb: str, driver_symbol_path: str, break_symbol: str, ms_cache_dir: str, reload_modules: list
) -> str:
    # dxgi.dll / d3d11.dll etc. are Windows system DLLs, symbolicated from Microsoft's
    # public symbol server rather than the driver PDB.
    ms_symbol_path = f"SRV*{ms_cache_dir}*{DEFAULT_MS_SYMBOL_SERVER}"
    # Targeted reload of just the relevant modules instead of blanket `.reload /f`,
    # which would otherwise try (and network-round-trip for) every one of the
    # ~300 modules chrome.exe loads and can time the whole session out.
    reload_lines = [f".reload /f {m}" for m in reload_modules]
    return "\n".join(
        [
            f".sympath {chrome_pdb};{driver_symbol_path};{ms_symbol_path}",
            *reload_lines,
            f"bp /1 {break_symbol}",
            "g",
            "kb",
            "qd",  # detach without killing the debuggee
        ]
    ) + "\n"


def run_cdb(cdb_path: str, pid: int, cdb_script: str, timeout: float) -> str:
    fd, cmd_file = tempfile.mkstemp(suffix=".wdcmd", text=True)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(cdb_script)

        cmd = [cdb_path, "-p", str(pid), "-cf", cmd_file]
        print("[cmd]", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=timeout
            )
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired as e:
            print(
                "[!] cdb timed out (the breakpoint may never have been hit). The cdb process was force-killed, "
                "but the Chrome GPU process may still be left in an interrupted/broken state -- please check it manually.",
                file=sys.stderr,
            )
            out = (e.stdout or "") + (e.stderr or "")
            return out
    finally:
        os.remove(cmd_file)


def extract_kb_section(output: str) -> str:
    lines = output.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("ChildEBP") or line.strip().startswith("Child-SP"):
            start = i
            break
    if start is None:
        return output
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return "\n".join(lines[start - 1 if start > 0 else start:end])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pid", type=int, default=None, help="Known GPU process PID; skips launching Chrome")
    parser.add_argument("--chrome-exe", default=DEFAULT_CHROME_EXE, help="Path to chrome.exe")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL to open when launching Chrome")
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=None,
        help="Extra Chrome command-line argument, repeatable. Default: --no-sandbox --enable-skia-graphite "
        "--skia-graphite-backend=dawn-d3d11 (forces D3D11, avoiding D3D12/igd12umd64)",
    )
    parser.add_argument(
        "--gpu-startup-dialog",
        action="store_true",
        help="Add --gpu-startup-dialog to Chrome (pops up a native dialog that blocks the GPU process, handy for manually confirming the PID)",
    )
    parser.add_argument(
        "--chrome-pdb", default=None,
        help="Local Chrome PDB directory. If given, use it; otherwise default to pulling from the Chromium symbol server at --chrome-symbol-server-url",
    )
    parser.add_argument(
        "--chrome-symbol-server-url", default=DEFAULT_CHROME_SYMBOL_SERVER,
        help="Chrome symbol server URL, only effective when --chrome-pdb is not provided",
    )
    parser.add_argument(
        "--chrome-symbol-cache-dir", default=DEFAULT_CHROME_SYMBOL_CACHE_DIR,
        help="Local cache directory for symbols downloaded from the Chrome symbol server, only effective when --chrome-pdb is not provided",
    )
    parser.add_argument(
        "--driver-pdb", default=None,
        help="Local driver PDB directory. If given, use it; otherwise default to pulling from the symbol server at --driver-symbol-server-url",
    )
    parser.add_argument(
        "--driver-symbol-server-url", default=DEFAULT_DRIVER_SYMBOL_SERVER, help="Driver symbol server URL, only effective when --driver-pdb is not provided"
    )
    parser.add_argument(
        "--driver-symbol-cache-dir", default=DEFAULT_DRIVER_SYMBOL_CACHE_DIR, help="Local cache directory for driver symbols downloaded from the symbol server"
    )
    parser.add_argument(
        "--ms-symbol-cache-dir", default=DEFAULT_MS_SYMBOL_CACHE_DIR, help="Local cache directory for Windows system DLL symbols (dxgi.dll etc.) downloaded from Microsoft's symbol server"
    )
    parser.add_argument(
        "--reload-module",
        action="append",
        default=None,
        help=f"Module name to target with .reload /f in cdb, repeatable. Default: {DEFAULT_RELOAD_MODULES} (avoids a blanket .reload /f scanning every module)",
    )
    parser.add_argument(
        "--prefetch",
        dest="prefetch",
        action="store_true",
        default=True,
        help="Before attaching, use symchk to prefetch dxgi.dll (+ files given via --prefetch-chrome-file / --prefetch-driver-dll) into the local cache; default ON",
    )
    parser.add_argument(
        "--no-prefetch",
        dest="prefetch",
        action="store_false",
        help="Disable prefetching; let cdb download symbols over the network on demand instead",
    )
    parser.add_argument(
        "--prefetch-dxgi-dll", default=DEFAULT_PREFETCH_DXGI_DLL, help="Local path to dxgi.dll used for prefetching"
    )
    parser.add_argument(
        "--prefetch-chrome-file",
        action="append",
        default=None,
        help="Local path to a Chrome file to prefetch (e.g. chrome.exe/chrome.dll), repeatable, only effective when --chrome-pdb is not provided",
    )
    parser.add_argument(
        "--prefetch-driver-dll",
        action="append",
        default=None,
        help="Local path to an driver dll to prefetch, repeatable (one --prefetch-driver-dll per file); if omitted, driver prefetching is skipped",
    )
    parser.add_argument("--windbg", default=DEFAULT_WINDBG, help="Path to windbg.exe, used to derive cdb.exe in the same directory")
    parser.add_argument("--cdb", default=None, help="Directly specify the path to cdb.exe (skips auto-detection)")
    parser.add_argument(
        "--break-symbol",
        default=DEFAULT_BREAK_SYMBOL,
        help="Driver symbol to break on; captures the stack as soon as it's hit once (default: the WaitForSynchronizationObjectFromCpu-style endpoint function)",
    )
    parser.add_argument("--find-gpu-timeout", type=float, default=30.0, help="Timeout (seconds) waiting for the GPU process to appear")
    parser.add_argument("--break-timeout", type=float, default=120.0, help="Timeout (seconds) waiting for the breakpoint to be hit")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory to write results to")
    args = parser.parse_args()

    extra_args = args.extra_arg if args.extra_arg is not None else [
        "--no-sandbox", "--enable-skia-graphite", "--skia-graphite-backend=dawn-d3d11"
    ]
    if args.gpu_startup_dialog:
        extra_args = extra_args + ["--gpu-startup-dialog"]

    os.makedirs(args.output_dir, exist_ok=True)

    try:
        cdb_path = args.cdb or find_cdb(args.windbg)
    except FileNotFoundError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    browser_proc = None
    try:
        if args.chrome_pdb:
            chrome_symbol_path = args.chrome_pdb
        else:
            os.makedirs(args.chrome_symbol_cache_dir, exist_ok=True)
            chrome_symbol_path = f"SRV*{args.chrome_symbol_cache_dir}*{args.chrome_symbol_server_url}"

        if args.pid is not None:
            pid = args.pid
            print(f"[*] Using manually specified PID: {pid}")
        else:
            browser_proc = launch_chrome(args.chrome_exe, args.url, extra_args)
            pid = find_gpu_pid(args.find_gpu_timeout)

        if args.driver_pdb:
            driver_symbol_path = args.driver_pdb
        else:
            os.makedirs(args.driver_symbol_cache_dir, exist_ok=True)
            driver_symbol_path = f"SRV*{args.driver_symbol_cache_dir}*{args.driver_symbol_server_url}"
        os.makedirs(args.ms_symbol_cache_dir, exist_ok=True)
        print(f"[*] Chrome symbol path:      {chrome_symbol_path}")
        print(f"[*] Driver symbol path:      {driver_symbol_path}")
        print(f"[*] MS symbol cache dir:     {args.ms_symbol_cache_dir}")
        print(f"[*] Output directory:        {args.output_dir}")

        if args.prefetch:
            symchk_path = find_symchk(args.windbg)
            prefetch_symbols(symchk_path, args.prefetch_dxgi_dll, DEFAULT_MS_SYMBOL_SERVER, args.ms_symbol_cache_dir)
            if not args.chrome_pdb and args.prefetch_chrome_file:
                for chrome_file in args.prefetch_chrome_file:
                    prefetch_symbols(symchk_path, chrome_file, args.chrome_symbol_server_url, args.chrome_symbol_cache_dir)
            if not args.driver_pdb and args.prefetch_driver_dll:
                for driver_dll in args.prefetch_driver_dll:
                    prefetch_symbols(symchk_path, driver_dll, args.driver_symbol_server_url, args.driver_symbol_cache_dir)

        reload_modules = args.reload_module if args.reload_module is not None else DEFAULT_RELOAD_MODULES
        cdb_script = build_cdb_script(chrome_symbol_path, driver_symbol_path, args.break_symbol, args.ms_symbol_cache_dir, reload_modules)
        output = run_cdb(cdb_path, pid, cdb_script, args.break_timeout)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_log = os.path.join(args.output_dir, f"stack_raw_{timestamp}.txt")
        with open(raw_log, "w", encoding="utf-8") as f:
            f.write(output)

        kb_section = extract_kb_section(output)
        kb_log = os.path.join(args.output_dir, f"stack_kb_{timestamp}.txt")
        with open(kb_log, "w", encoding="utf-8") as f:
            f.write(kb_section)

        print("=" * 60)
        print(kb_section)
        print("=" * 60)
        print(f"[*] Full cdb output: {raw_log}")
        print(f"[*] Extracted stack: {kb_log}")
        return 0
    finally:
        if browser_proc is not None:
            print(
                "[*] The Chrome browser process is still running (the GPU process was cleanly detached with qd); "
                "close it manually or run taskkill /IM chrome.exe /F if needed"
            )


if __name__ == "__main__":
    sys.exit(main())
