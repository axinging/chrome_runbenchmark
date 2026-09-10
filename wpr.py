#!/usr/bin/env python3
"""Replay a Chromium WPR (.wprgo) archive and open it directly in Chrome,
without going through tools/perf/run_benchmark.

Usage:
  python run_on_wpr.py --archive path\\to\\rendering_desktop_004.wprgo ^
      --url http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html
"""

import argparse
import getpass
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

DEFAULT_CHROME = (
    rf"C:\Users\{getpass.getuser()}\AppData\Local\Google\Chrome SxS\Application\chrome.exe"
)
DEFAULT_ARGS = "--disable-skia-graphite --show-fps-counter"
DEFAULT_WPR_DIR = r"D:\graphiteperf\chromium\src\third_party\webpagereplay"

PORT_RE = re.compile(
    r"Starting server on (?P<proto>http|https)://[^:]*:(?P<port>\d+)"
)


class WprServer:
  def __init__(self, wpr_dir, archive_path, http_port=0, https_port=0):
    self.wpr_dir = wpr_dir
    self.archive_path = archive_path
    self.http_port = http_port
    self.https_port = https_port
    self.process = None
    self.ports = {}
    self._lines = []
    self._lock = threading.Lock()

  def _build_cmd(self):
    return [
        sys.executable,
        str(self.wpr_dir / "scripts" / "run_wpr.py"),
        "replay",
        f"--http_port={self.http_port}",
        f"--https_port={self.https_port}",
        f"--https_key_file={self.wpr_dir / 'wpr_key.pem'}",
        f"--https_cert_file={self.wpr_dir / 'wpr_cert.pem'}",
        f"--inject_scripts={self.wpr_dir / 'deterministic.js'}",
        str(self.archive_path),
    ]

  def _pump_output(self):
    for raw_line in self.process.stdout:
      line = raw_line.decode(errors="replace").rstrip()
      with self._lock:
        self._lines.append(line)
      print(f"[wpr] {line}")

  def start(self, timeout=60):
    cmd = self._build_cmd()
    print("Starting WPR replay:", " ".join(cmd))
    self.process = subprocess.Popen(
        cmd,
        cwd=self.wpr_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    threading.Thread(target=self._pump_output, daemon=True).start()

    deadline = time.time() + timeout
    while time.time() < deadline:
      if self.process.poll() is not None:
        raise RuntimeError(
            f"wpr process exited early with code {self.process.returncode}"
        )
      with self._lock:
        for line in self._lines:
          m = PORT_RE.search(line)
          if m:
            self.ports[m.group("proto")] = int(m.group("port"))
      if "http" in self.ports and "https" in self.ports:
        return self.ports
      time.sleep(0.2)
    raise TimeoutError("Timed out waiting for wpr to report its ports")

  def stop(self):
    if not self.process or self.process.poll() is not None:
      return
    # Ask wpr to exit gracefully (restores DNS config); fall back to kill.
    try:
      urllib.request.urlopen(
          f"http://127.0.0.1:{self.ports['http']}/web-page-replay-command-exit",
          timeout=5,
      )
    except Exception:
      pass
    try:
      self.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
      self.process.terminate()
      try:
        self.process.wait(timeout=5)
      except subprocess.TimeoutExpired:
        self.process.kill()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--archive", required=True, help="Path to the .wprgo archive to replay"
  )
  parser.add_argument(
      "--url",
      required=True,
      help="Exact URL recorded in the archive (scheme/host/path must match)",
  )
  parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Path to chrome.exe")
  parser.add_argument(
      "--args", default=DEFAULT_ARGS, help="Extra chrome flags, space separated"
  )
  parser.add_argument(
      "--wpr-dir",
      default=DEFAULT_WPR_DIR,
      help="Path to the third_party/webpagereplay checkout",
  )
  parser.add_argument("--http-port", type=int, default=0)
  parser.add_argument("--https-port", type=int, default=0)
  parser.add_argument(
      "--user-data-dir",
      default=None,
      help="Chrome profile dir (default: a fresh temp dir)",
  )
  args = parser.parse_args()

  archive_path = Path(args.archive).resolve()
  if not archive_path.exists():
    parser.error(f"archive not found: {archive_path}")

  wpr_dir = Path(args.wpr_dir).resolve()
  if not (wpr_dir / "scripts" / "run_wpr.py").exists():
    parser.error(
        f"scripts\\run_wpr.py not found under {wpr_dir}.\n"
        "Sync it first: cd D:\\graphiteperf\\chromium\\src && gclient sync"
    )

  user_data_dir = args.user_data_dir or tempfile.mkdtemp(prefix="wpr_chrome_profile_")

  server = WprServer(wpr_dir, archive_path, args.http_port, args.https_port)
  ports = server.start()
  print(f"WPR replay ready: http={ports['http']} https={ports['https']}")

  chrome_cmd = (
      [
          args.chrome,
          f'--proxy-server=http=127.0.0.1:{ports["http"]};https=127.0.0.1:{ports["https"]}',
          # wpr terminates HTTPS with its own cert (wpr_cert.pem); ignore the
          # resulting cert errors instead of installing it as a trusted root.
          "--ignore-certificate-errors",
          f"--user-data-dir={user_data_dir}",
      ]
      + shlex.split(args.args)
      + [args.url]
  )

  print("Launching Chrome:", " ".join(chrome_cmd))
  chrome_proc = subprocess.Popen(chrome_cmd)

  try:
    input("Chrome is running against the WPR replay. Press Enter here to stop...\n")
  except KeyboardInterrupt:
    pass
  finally:
    if chrome_proc.poll() is None:
      chrome_proc.terminate()
    server.stop()
    print("Stopped.")


if __name__ == "__main__":
  main()
