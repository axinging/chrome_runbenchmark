#!/usr/bin/env python3
"""Manage proxy settings for: Windows Internet Settings, git global config, http(s)_proxy env vars.

Usage:
    python proxy.py set                       # write proxy only where not already configured
    python proxy.py override                  # always (re)write proxy, regardless of current value
    python proxy.py unset                     # remove/disable proxy everywhere
    python proxy.py help                      # print copy-pasteable manual set/unset commands
    python proxy.py backup                    # save current proxy state to a JSON file
    python proxy.py restore                   # write proxy state back from that JSON file

Backup/restore:
    - "backup" reads the current state of the selected --targets (windows/git/env) and writes it
      to --file (default: proxy.json next to this script). If --targets is a subset, only those
      keys are updated in the file; the rest of an existing file is left untouched.
    - "restore" always overwrites the current state with whatever is in the file (like "override"),
      including writing back an unset/empty target if that's what was backed up. Missing keys in
      the file (because they were never backed up) are left alone.

Address resolution:
    - If env var ALL_PROXY_ADDRESS is non-empty, it is used for every target (windows/git/http/https).
      If it is not set (and --address is not passed), it defaults to DEFAULT_ALL_PROXY_ADDRESS below.
    - Otherwise each target uses its own address, read from (in priority order) the matching
      --xxx-address CLI flag, then the matching env var:
          windows -> --windows-address / WINDOWS_PROXY_ADDRESS
          git     -> --git-address     / GIT_PROXY_ADDRESS
          http_proxy  -> --http-address  / HTTP_PROXY_ADDRESS
          https_proxy -> --https-address / HTTPS_PROXY_ADDRESS
    - A target with no resolved address is skipped (with a message) for set/override.

Notes:
    - Windows proxy + env var changes are written under HKEY_CURRENT_USER, so no elevation is needed.
    - The persisted env vars are named lowercase (http_proxy/https_proxy): Windows env lookups are
      case-insensitive so native tools still find them, and Bash-based tools (Git Bash/MSYS2/WSL)
      expect lowercase. A registry value name is one case-insensitive slot, so an uppercase copy
      would just collapse into the same entry rather than coexist.
    - "git" requires the git executable to be on PATH.
"""

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys

if sys.platform != "win32":
    sys.exit("This script only supports Windows.")

import winreg

# Fallback addresses used when no explicit address is provided via --address/env var.
DEFAULT_ALL_PROXY_ADDRESS = "http://proxy-.com:911"
DEFAULT_WINDOWS_PROXY_ADDRESS = DEFAULT_ALL_PROXY_ADDRESS
DEFAULT_GIT_PROXY_ADDRESS = DEFAULT_ALL_PROXY_ADDRESS
DEFAULT_HTTP_PROXY_ADDRESS = DEFAULT_ALL_PROXY_ADDRESS
DEFAULT_HTTPS_PROXY_ADDRESS = DEFAULT_ALL_PROXY_ADDRESS

INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
ENVIRONMENT_KEY = "Environment"

# Default backup/restore file: proxy.json next to this script.
DEFAULT_BACKUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.json")


# --------------------------------------------------------------------------
# Windows system proxy (HKCU Internet Settings)
# --------------------------------------------------------------------------

def get_windows_proxy():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, winreg.KEY_READ) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                server = ""
            return bool(enable), server
    except FileNotFoundError:
        return False, ""


def _strip_scheme(address):
    return re.sub(r"^\w+://", "", address).rstrip("/")


def windows_proxy_server_value(address):
    stripped = _strip_scheme(address)
    return f"http={stripped};https={stripped}"


def set_windows_proxy(address):
    server = windows_proxy_server_value(address)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
    _refresh_windows_internet_settings()
    return server


def set_windows_proxy_raw(enabled, server):
    """Write ProxyEnable/ProxyServer exactly as given, without address normalization (for restore)."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enabled else 0)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server or "")
    _refresh_windows_internet_settings()


def unset_windows_proxy():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        try:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, "")
        except OSError:
            pass
    _refresh_windows_internet_settings()


def _refresh_windows_internet_settings():
    INTERNET_OPTION_SETTINGS_CHANGED = 39
    INTERNET_OPTION_REFRESH = 37
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
    wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)


def apply_windows(action, address):
    enabled, server = get_windows_proxy()
    currently_set = enabled and bool(server)

    if action == "unset":
        unset_windows_proxy()
        print("[windows] proxy disabled")
        return

    if action == "set" and currently_set:
        print(f"[windows] already set ({server}), skip")
        return

    if not address:
        print("[windows] no address resolved, skip")
        return

    server = set_windows_proxy(address)
    print(f"[windows] proxy set to {server}")


# --------------------------------------------------------------------------
# git global config (http.proxy / https.proxy)
# --------------------------------------------------------------------------

def get_git_proxy(key):
    try:
        result = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        print("[git] git executable not found on PATH, skip")
        return None
    return result.stdout.strip() if result.returncode == 0 else ""


def set_git_proxy(key, address):
    subprocess.run(["git", "config", "--global", key, address], check=True)


def unset_git_proxy(key):
    subprocess.run(["git", "config", "--global", "--unset", key], capture_output=True, text=True)


def apply_git(action, address):
    for key in ("http.proxy", "https.proxy"):
        current = get_git_proxy(key)
        if current is None:
            return  # git not available, no point retrying for https.proxy either

        if action == "unset":
            unset_git_proxy(key)
            print(f"[git] {key} unset")
            continue

        if action == "set" and current:
            print(f"[git] {key} already set ({current}), skip")
            continue

        if not address:
            print(f"[git] no address resolved for {key}, skip")
            continue

        set_git_proxy(key, address)
        print(f"[git] {key} set to {address}")


# --------------------------------------------------------------------------
# Persistent user environment variables (HKCU\Environment)
# --------------------------------------------------------------------------

def get_persistent_env(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return ""


def set_persistent_env(name, value):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    _broadcast_env_change()
    os.environ[name] = value


def unset_persistent_env(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass
    _broadcast_env_change()
    os.environ.pop(name, None)


def _broadcast_env_change():
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x1A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
    )


def apply_env(action, http_address, https_address):
    # Lowercase names: Windows env var lookups are case-insensitive (so Windows-native tools
    # still find these), but Bash-based tools (Git Bash/MSYS2/WSL) are case-sensitive and expect
    # lowercase. A registry value name is a single case-insensitive slot, so writing both an
    # upper- and lowercase entry would just collapse into one anyway - lowercase covers both.
    for name, address in (("http_proxy", http_address), ("https_proxy", https_address)):
        current = get_persistent_env(name)

        if action == "unset":
            unset_persistent_env(name)
            print(f"[env] {name} unset")
            continue

        if action == "set" and current:
            print(f"[env] {name} already set ({current}), skip")
            continue

        if not address:
            print(f"[env] no address resolved for {name}, skip")
            continue

        set_persistent_env(name, address)
        print(f"[env] {name} set to {address}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def resolve_addresses(args):
    all_address = (args.address or os.environ.get("ALL_PROXY_ADDRESS", "") or DEFAULT_ALL_PROXY_ADDRESS).strip()
    if all_address:
        return {"windows": all_address, "git": all_address, "http": all_address, "https": all_address}

    return {
        "windows": (args.windows_address or os.environ.get("WINDOWS_PROXY_ADDRESS", "") or DEFAULT_WINDOWS_PROXY_ADDRESS).strip(),
        "git": (args.git_address or os.environ.get("GIT_PROXY_ADDRESS", "") or DEFAULT_GIT_PROXY_ADDRESS).strip(),
        "http": (args.http_address or os.environ.get("HTTP_PROXY_ADDRESS", "") or DEFAULT_HTTP_PROXY_ADDRESS).strip(),
        "https": (args.https_address or os.environ.get("HTTPS_PROXY_ADDRESS", "") or DEFAULT_HTTPS_PROXY_ADDRESS).strip(),
    }


def print_manual_commands(addresses, targets):
    """Print copy-pasteable cmd.exe commands for setting/unsetting each proxy target by hand."""

    if "windows" in targets:
        server = windows_proxy_server_value(addresses["windows"] or DEFAULT_WINDOWS_PROXY_ADDRESS)
        print("# --- Windows system proxy ---")
        print("# set:")
        print(f'reg add "HKCU\\{INTERNET_SETTINGS_KEY}" /v ProxyEnable /t REG_DWORD /d 1 /f')
        print(f'reg add "HKCU\\{INTERNET_SETTINGS_KEY}" /v ProxyServer /t REG_SZ /d "{server}" /f')
        print("# unset:")
        print(f'reg add "HKCU\\{INTERNET_SETTINGS_KEY}" /v ProxyEnable /t REG_DWORD /d 0 /f')
        print(f'reg add "HKCU\\{INTERNET_SETTINGS_KEY}" /v ProxyServer /t REG_SZ /d "" /f')
        print()

    if "git" in targets:
        address = addresses["git"] or DEFAULT_GIT_PROXY_ADDRESS
        print("# --- git global config ---")
        print("# set:")
        print(f'git config --global http.proxy "{address}"')
        print(f'git config --global https.proxy "{address}"')
        print("# unset:")
        print('git config --global --unset http.proxy')
        print('git config --global --unset https.proxy')
        print()

    if "env" in targets:
        http_address = addresses["http"] or DEFAULT_HTTP_PROXY_ADDRESS
        https_address = addresses["https"] or DEFAULT_HTTPS_PROXY_ADDRESS
        print("# --- user environment variables (persistent) ---")
        print("# set:")
        print(f'setx http_proxy "{http_address}"')
        print(f'setx https_proxy "{https_address}"')
        print("# unset:")
        print('reg delete "HKCU\\Environment" /v http_proxy /f')
        print('reg delete "HKCU\\Environment" /v https_proxy /f')
        print()

    print("# Note: reg/setx changes only apply to new processes started after the change")
    print("# (e.g. open a new terminal window). Re-run this script instead if you want the")
    print("# running process + broadcast notification handled automatically.")
    print()
    print("# --- backup / restore current state (via this script, not raw shell commands) ---")
    print(f'python "{os.path.abspath(__file__)}" backup')
    print(f'python "{os.path.abspath(__file__)}" restore')


# --------------------------------------------------------------------------
# Backup / restore (JSON snapshot of all three targets)
# --------------------------------------------------------------------------

def do_backup(path, targets):
    state = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            state = {}

    if "windows" in targets:
        enabled, server = get_windows_proxy()
        state["windows"] = {"enabled": enabled, "server": server}
        print(f"[backup] windows: enabled={enabled} server={server!r}")

    if "git" in targets:
        http_value = get_git_proxy("http.proxy")
        https_value = get_git_proxy("https.proxy")
        if http_value is None or https_value is None:
            print("[backup] git: git executable not found on PATH, skip")
        else:
            state["git"] = {"http.proxy": http_value, "https.proxy": https_value}
            print(f"[backup] git: http.proxy={http_value!r} https.proxy={https_value!r}")

    if "env" in targets:
        http_env = get_persistent_env("http_proxy")
        https_env = get_persistent_env("https_proxy")
        state["env"] = {"http_proxy": http_env, "https_proxy": https_env}
        print(f"[backup] env: http_proxy={http_env!r} https_proxy={https_env!r}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"[backup] written to {path}")


def do_restore(path, targets):
    if not os.path.exists(path):
        print(f"[restore] file not found: {path}")
        return

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    if "windows" in targets:
        windows_state = state.get("windows")
        if windows_state is None:
            print("[restore] windows: no data in backup file, skip")
        else:
            enabled = bool(windows_state.get("enabled"))
            server = windows_state.get("server") or ""
            set_windows_proxy_raw(enabled, server)
            print(f"[restore] windows: enabled={enabled} server={server!r}")

    if "git" in targets:
        git_state = state.get("git")
        if git_state is None:
            print("[restore] git: no data in backup file, skip")
        elif get_git_proxy("http.proxy") is None:
            print("[restore] git: git executable not found on PATH, skip")
        else:
            for key in ("http.proxy", "https.proxy"):
                value = git_state.get(key) or ""
                if value:
                    set_git_proxy(key, value)
                else:
                    unset_git_proxy(key)
                print(f"[restore] git: {key} -> {value or '(unset)'}")

    if "env" in targets:
        env_state = state.get("env")
        if env_state is None:
            print("[restore] env: no data in backup file, skip")
        else:
            for name in ("http_proxy", "https_proxy"):
                value = env_state.get(name) or ""
                if value:
                    set_persistent_env(name, value)
                else:
                    unset_persistent_env(name)
                print(f"[restore] env: {name} -> {value or '(unset)'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["set", "unset", "override", "help", "backup", "restore"])
    parser.add_argument("--address", help="Address used for all targets (overrides ALL_PROXY_ADDRESS env var)")
    parser.add_argument("--windows-address", help="Address for Windows system proxy")
    parser.add_argument("--git-address", help="Address for git http.proxy/https.proxy")
    parser.add_argument("--http-address", help="Address for http_proxy env var")
    parser.add_argument("--https-address", help="Address for https_proxy env var")
    parser.add_argument(
        "--targets", default="windows,git,env",
        help="Comma-separated subset of: windows,git,env (default: all)",
    )
    parser.add_argument(
        "--file", default=DEFAULT_BACKUP_FILE,
        help=f"Backup/restore JSON file path (default: {DEFAULT_BACKUP_FILE})",
    )
    args = parser.parse_args()

    targets = {t.strip() for t in args.targets.split(",") if t.strip()}

    if args.action == "backup":
        do_backup(args.file, targets)
        return

    if args.action == "restore":
        do_restore(args.file, targets)
        return

    addresses = resolve_addresses(args)

    if args.action == "help":
        print_manual_commands(addresses, targets)
        return

    if "windows" in targets:
        apply_windows(args.action, addresses["windows"])
    if "git" in targets:
        apply_git(args.action, addresses["git"])
    if "env" in targets:
        apply_env(args.action, addresses["http"], addresses["https"])


if __name__ == "__main__":
    main()
