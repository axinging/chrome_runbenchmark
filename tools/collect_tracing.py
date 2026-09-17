#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动采集 Chrome tracing 脚本
----------------------------------
对 URLS x CONFIGS 的每一种组合，启动一个全新的 Chrome 实例，
使用 Chrome 内置的 startup tracing（--trace-startup）采集 TRACE_DURATION 秒，
把结果写到 tracing-<日期> 文件夹里。

文件名规则： <URLS里的label>__<配置名>__<时间>.json
配置名规则：
  --disable-skia-graphite                              -> ganesh
  --enable-skia-graphite --skia-graphite-backend=dawn-d3d11 -> graphit-d3d11
  --enable-skia-graphite --skia-graphite-backend=dawn-d3d12 -> graphit-d3d12

采集期间被测页面是唯一前台窗口，避免后台节流影响结果。
"""

import os
import re
import sys
import json
import time
import shutil
import subprocess
import datetime

# ============================ 可修改配置 ============================

# 脚本所在目录，输出根目录 / profile 根目录都基于它，脚本可挪到任意机器/路径下直接跑。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 当前登录用户名，用来拼 Chrome Canary 的默认安装路径。
CURRENT_USER = os.environ.get("USERNAME") or os.getlogin()

CHROME = rf"C:\Users\{CURRENT_USER}\AppData\Local\Google\Chrome SxS\Application\chrome.exe"

# 输出根目录（脚本会在其下再建一个 tracing-<日期时间>[-<tag>] 子文件夹）
BASE_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "tracing")

# 输出文件夹名的 tag，默认空。非空时会拼到文件夹名末尾，便于区分不同批次。
OUTPUT_TAG = ""

# 固定 profile 的根目录：每个配置用一个固定子目录（如 profiles\ganesh）。
# 这样同一配置多次运行复用同一份 cache，配置之间互不污染。
PROFILE_ROOT = os.path.join(BASE_OUTPUT_DIR, "profiles")

# 每次采集时长（秒）
TRACE_DURATION = 30

# 采集结束后，等待 trace 写盘的最长时间（秒）。大 category 文件可能有几百 MB，
# 写盘 + 重命名需要较久，脚本会轮询直到文件出现且大小稳定，最多等这么久。
MAX_FLUSH_WAIT = 180
# 文件大小连续这么多秒不变，就认为写盘完成
STABLE_SECS = 3

# tracing 抓取的 category，可按需增删。
# 注意（已实测）：
#   - disabled-by-default-skia.gpu 会导致 startup trace 不落盘，切勿加入！
#   - disabled-by-default-skia 可用，但数据量极大（约 30MB/秒），需要时再加。
TRACE_CATEGORIES = (
    "benchmark,toplevel,viz,gpu,cc,skia,"
    #"disabled-by-default-skia,"
    # Ganesh 渲染经 GL/ANGLE -> gpu.service 记录 GL 命令(glClear/glDrawElements/scissor/FBO)，
    # 用来验证 leading 假设："dcomp 每帧重绘面积是否 >> 719x719 damage"。
    # 注意：这是 gpu.service，不是会破坏 startup trace 的 disabled-by-default-skia.gpu；
    # 但仍请跑完确认 .json 正常落盘(>10MB)，若产出为空/极小就删掉本行再跑。
    "disabled-by-default-gpu.service,"
    "disabled-by-default-gpu.dawn,disabled-by-default-gpu.graphite.dawn"
)

# 要测的网页（可以放多个）。key 是输出文件名里用的 label，
# 同一个 URL 想跑多种场景（比如重复对照）时靠 label 区分，不能靠 URL 本身区分。
URLS = {
    "animometer_webgl": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html",
    "animometer_webgl_attrib_arrays": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html?use_attributes=1",
    "animometer_webgl_fast_call": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html",
    "animometer_webgl_indexed": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&num_geometries=120000",
    "animometer_webgl_indexed_fast_call": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&num_geometries=120000",
    "animometer_webgl_indexed_multi_draw": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&use_multi_draw=1&num_geometries=120000",
    "animometer_webgl_indexed_multi_draw_base_vertex_base_instance": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl-indexed-instanced.html?webgl_version=2&use_attributes=1&use_multi_draw=1&use_base_vertex_base_instance=1&num_geometries=120000",
    "animometer_webgl_multi_draw": "http://kenrussell.github.io/webgl-animometer/Animometer/tests/3d/webgl.html?webgl_version=2&use_ubos=1&use_multi_draw=1",
    "aquarium": "http://webglsamples.org/aquarium/aquarium.html",
    "aquarium_20k": "http://webglsamples.org/aquarium/aquarium.html?numFish=20000",
    "aquarium_20k_fast_call": "http://webglsamples.org/aquarium/aquarium.html?numFish=20000",
}

# 要测的浏览器配置（每个是附加到命令行的一组 flags）
# 暂不测 D3D12（其 submit/present 有单独的 CPU 开销问题，另行跟踪）
#CONFIGS = [
#    ["--disable-skia-graphite"],
#    ["--disable-skia-graphite", "--disable-direct-composition"],
#]
CONFIGS = [
    ["--disable-skia-graphite"],
    ["--enable-skia-graphite"],
]

# ==================================================================


def config_label(flags):
    """根据 flags 推断配置名。"""
    joined = " ".join(flags)
    if "--disable-skia-graphite" in joined and "--disable-direct-composition" in joined:
        return "ganesh-disabledc"
    if "--disable-skia-graphite" in joined:
        return "ganesh"
    if "--enable-skia-graphite" in joined and "--skia-graphite-backend=dawn-d3d11" in joined:
        return "graphit-d3d11"
    if "--enable-skia-graphite" in joined and "--skia-graphite-backend=dawn-d3d12" in joined:
        return "graphit-d3d12"
    if "--enable-skia-graphite" in joined:
        return "graphit-d3d12"
    return "unknown"


def sanitize(text):
    """把非字母数字字符替换成下划线，避免非法文件名。"""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def build_output_name(label, flags, ts):
    cfg = config_label(flags)
    return "__".join([sanitize(label), cfg, ts]) + ".json"


def clean_session(user_data_dir):
    """删除 profile 里的 session 文件，避免恢复上一次的旧 tab。
    只删 session，不动 cache / Cookies / 其它数据。"""
    default_dir = os.path.join(user_data_dir, "Default")
    # 整个 Sessions 目录
    shutil.rmtree(os.path.join(default_dir, "Sessions"), ignore_errors=True)
    # 顶层 session 快照文件
    for fname in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        try:
            os.remove(os.path.join(default_dir, fname))
        except OSError:
            pass


def fix_exit_type(user_data_dir):
    """把 Preferences 里的 exit_type 改成 Normal，避免上次被强杀后
    出现 “Chrome 未正确关闭 / 恢复页面” 的气泡。best-effort。"""
    prefs_path = os.path.join(user_data_dir, "Default", "Preferences")
    if not os.path.exists(prefs_path):
        return
    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        prefs.setdefault("profile", {})
        prefs["profile"]["exit_type"] = "Normal"
        prefs["profile"]["exited_cleanly"] = True
        with open(prefs_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except (OSError, ValueError):
        pass


def wait_for_file_complete(path, max_wait, stable_secs):
    """轮询等待 trace 文件写盘完成：文件先要出现，然后大小连续 stable_secs 秒不变。
    返回 True 表示写盘完成，False 表示超时。"""
    waited = 0
    last_size = -1
    stable = 0
    while waited < max_wait:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 0 and size == last_size:
                stable += 1
                if stable >= stable_secs:
                    return True
            else:
                stable = 0
            last_size = size
            mb = size / (1024 * 1024)
            print(f"      写盘中... {mb:8.1f} MB (稳定 {stable}/{stable_secs}s)", end="\r", flush=True)
        else:
            print(f"      等待文件生成... {waited:>3d}s", end="\r", flush=True)
        time.sleep(1)
        waited += 1
    return False


def kill_chrome_for_profile(user_data_dir):
    """按 --user-data-dir 精确匹配命令行，杀掉用这个 profile 的所有 chrome.exe。
    只杀目标 profile 的实例，不会误伤你其它 Chrome。"""
    pattern = "*--user-data-dir={}*".format(user_data_dir)
    ps = (
        "$p='{}';"
        "Get-CimInstance Win32_Process | "
        "? {{$_.Name -eq 'chrome.exe' -and $_.CommandLine -like $p}} | "
        "% {{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}"
    ).format(pattern)
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_one(label, url, flags, out_dir, ts):
    out_name = build_output_name(label, flags, ts)
    out_path = os.path.join(out_dir, out_name)

    # 每个配置用固定 profile，保证 cache 一致；启动前清掉 session，避免旧 tab 恢复。
    user_data_dir = os.path.join(PROFILE_ROOT, config_label(flags))
    os.makedirs(user_data_dir, exist_ok=True)
    # 先杀掉可能残留的、占用该 profile 的旧实例（否则新进程会 handoff、flags 失效）
    kill_chrome_for_profile(user_data_dir)
    time.sleep(1)
    clean_session(user_data_dir)
    fix_exit_type(user_data_dir)

    cmd = [
        CHROME,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--start-maximized",
        f"--user-data-dir={user_data_dir}",
        "--trace-startup=" + TRACE_CATEGORIES,
        f"--trace-startup-duration={TRACE_DURATION}",
        f"--trace-startup-file={out_path}",
        "--trace-startup-format=json",
    ] + list(flags) + [url]

    print(f"\n[RUN] {config_label(flags)}  <-  {label} ({url})")
    print(f"      输出: {out_path}")
    print(f"      命令: {' '.join(cmd)}")

    subprocess.Popen(cmd)
    completed = False
    try:
        # 1) 先等采集时长。Chrome 在 duration 结束时才开始把整个缓冲区写盘。
        for remaining in range(TRACE_DURATION, 0, -1):
            print(f"      采集中... 剩余 {remaining:>3d}s", end="\r", flush=True)
            time.sleep(1)
        print(" " * 50, end="\r")
        # 2) 再轮询等文件写盘完成（大文件可能几百 MB，需几十秒），期间不要杀进程！
        completed = wait_for_file_complete(out_path, MAX_FLUSH_WAIT, STABLE_SECS)
        print(" " * 50, end="\r")
        if not completed:
            print(f"      [WARN] 等待 {MAX_FLUSH_WAIT}s 仍未见文件稳定，可能文件过大或未生成")
    finally:
        # 文件写盘完成后，再按 profile 精确杀掉本轮的 chrome 实例
        kill_chrome_for_profile(user_data_dir)
        time.sleep(2)  # 给 Chrome 退出留点时间；固定 profile 保留不删

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        print(f"      [OK] 生成 {out_name}  ({size_mb:.1f} MB)")
        return True
    else:
        print(f"      [FAIL] 未生成 trace 文件: {out_path}")
        return False


def main():
    if not os.path.exists(CHROME):
        print(f"找不到 Chrome: {CHROME}")
        sys.exit(1)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder = f"tracing-{stamp}"
    if OUTPUT_TAG.strip():
        folder += f"-{sanitize(OUTPUT_TAG)}"
    out_dir = os.path.join(BASE_OUTPUT_DIR, folder)
    os.makedirs(out_dir, exist_ok=True)
    print(f"输出目录: {out_dir}")

    total = len(URLS) * len(CONFIGS)
    ok = 0
    idx = 0
    for label, url in URLS.items():
        for flags in CONFIGS:
            idx += 1
            print(f"\n===== [{idx}/{total}] =====")
            ts = datetime.datetime.now().strftime("%H%M%S")
            if run_one(label, url, flags, out_dir, ts):
                ok += 1

    print(f"\n完成: {ok}/{total} 成功。文件在 {out_dir}")


if __name__ == "__main__":
    main()
