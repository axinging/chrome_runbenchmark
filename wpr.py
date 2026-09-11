#!/usr/bin/env python3
"""Replay a Chromium WPR (.wprgo) archive and open it directly in Chrome,
without going through tools/perf/run_benchmark.

wpr.py is expected to live next to the chromium checkout, i.e.:
  <root>/wpr.py
  <root>/chromium/src/...
"""

import argparse
import getpass
import json
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CHROMIUM_SRC = SCRIPT_DIR / "chromium" / "src"

DEFAULT_CHROME = (
    rf"C:\Users\{getpass.getuser()}\AppData\Local\Google\Chrome SxS\Application\chrome.exe"
)
DEFAULT_ARGS = "--disable-skia-graphite --show-fps-counter"

PORT_RE = re.compile(
    r"Starting server on (?P<proto>http|https)://[^:]*:(?P<port>\d+)"
)

EPILOG = r"""
examples:
  # Give the URL directly (must match the URL recorded in the archive exactly).
  python wpr.py --archive chromium\src\tools\perf\page_sets\data\rendering_desktop_004.wprgo ^
      --url http://ie.microsoft.com/testdrive/Graphics/WorkerFountains/Default.html

  # Or just give the story's BASE_NAME and let wpr.py look up the URL (and the
  # archive, if --archive is omitted) under tools/perf/page_sets.
  python wpr.py --story microsoft_worker_fountains

  # Run Graphite instead of the default (Ganesh):
  python wpr.py --story microsoft_worker_fountains --args "--enable-skia-graphite --show-fps-counter"

how it works:
  1. Starts a WPR (Web Page Replay) server that replays the given .wprgo
     archive on two local ports (HTTP/HTTPS), chosen automatically.
  2. Launches Chrome with --proxy-server pointed at those ports, so all of
     Chrome's traffic goes through the replay server instead of the real
     network.
  3. Navigates straight to the recorded URL. Only URLs/domains that were
     captured in the archive will load; anything else will fail to load,
     which is expected -- WPR intercepts all traffic once it's running.
  4. Press Enter in this terminal to close Chrome and stop the replay server.

--story resolution:
  --story looks up a page_sets story by its BASE_NAME under
  <chromium-src>/tools/perf/page_sets/**/*.py to find its recorded URL, and
  under <chromium-src>/tools/perf/page_sets/data/*.json to find which
  .wprgo archive contains it (same lookup described in wpr.md). If --url or
  --archive is also given explicitly, that value wins and is not resolved.
  If the story exists in more than one benchmark's data index (e.g. both
  rendering_desktop.json and rendering_mobile.json), pass --benchmark to
  pick one explicitly; otherwise wpr.py defaults to the desktop index.
"""


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


def _splice(src, dst):
  try:
    while True:
      chunk = src.recv(65536)
      if not chunk:
        break
      dst.sendall(chunk)
  except OSError:
    pass
  finally:
    try:
      dst.shutdown(socket.SHUT_WR)
    except OSError:
      pass


class ConnectUnwrapProxy:
  """Strips the CONNECT preamble Chrome sends before WPR's TLS listener.

  Chrome's --proxy-server=https=host:port always talks to that address
  using the standard HTTP CONNECT method for https:// requests (e.g.
  "CONNECT www.ebay.com:443 HTTP/1.1\\r\\n...") -- that's true for any
  proxy given under the "https=" scheme mapping, not a WPR quirk. But
  WPR's https listener expects the connection's very first bytes to be a
  raw TLS ClientHello (it has no CONNECT handling anywhere in its Go
  source) and drops the connection with "first record does not look like
  a TLS handshake" the moment it sees Chrome's plaintext CONNECT line.
  Chrome then reports this as ERR_EMPTY_RESPONSE / "This page isn't
  working" -- for every HTTPS site, since every HTTPS navigation goes
  through this same path.

  This shim sits between Chrome and WPR's https port: it consumes the
  CONNECT request, replies 200, then splices the raw socket through so
  the next bytes WPR sees are the real TLS ClientHello.
  """

  def __init__(self, target_port):
    self.target_port = target_port
    self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self._sock.bind(("127.0.0.1", 0))
    self._sock.listen(128)
    self.port = self._sock.getsockname()[1]

  def start(self):
    threading.Thread(target=self._accept_loop, daemon=True).start()

  def _accept_loop(self):
    while True:
      try:
        conn, _ = self._sock.accept()
      except OSError:
        return
      threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

  def _handle(self, conn):
    try:
      conn.settimeout(10)
      buf = b""
      while b"\r\n\r\n" not in buf and len(buf) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
          conn.close()
          return
        buf += chunk
      if buf[:8].upper().startswith(b"CONNECT "):
        _head, _, leftover = buf.partition(b"\r\n\r\n")
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
      else:
        # Not a CONNECT preamble (e.g. a client dialing TLS directly);
        # pass through whatever was already read, untouched.
        leftover = buf
      upstream = socket.create_connection(("127.0.0.1", self.target_port), timeout=10)
      if leftover:
        upstream.sendall(leftover)
      conn.settimeout(None)
      upstream.settimeout(None)
      threading.Thread(target=_splice, args=(conn, upstream), daemon=True).start()
      threading.Thread(target=_splice, args=(upstream, conn), daemon=True).start()
    except OSError:
      conn.close()

  def stop(self):
    try:
      self._sock.close()
    except OSError:
      pass


def _iter_class_blocks(text):
  """Split a page_sets .py file's text into per-class chunks.

  RenderingStory.__init__ (page_sets/rendering/rendering_story.py) builds the
  real story name as BASE_NAME + ('_' + YEAR if YEAR else ''), so a class's
  literal BASE_NAME attribute is not always the full story name (e.g.
  BASE_NAME='ebay_pinch' + YEAR='2018' -> story name 'ebay_pinch_2018').
  Splitting into class blocks lets us read BASE_NAME/YEAR/URL together per
  class instead of matching BASE_NAME as a bare string.
  """
  positions = [m.start() for m in re.finditer(r"(?m)^class\s+\w+", text)]
  positions.append(len(text))
  for i in range(len(positions) - 1):
    yield text[positions[i]:positions[i + 1]]


def _story_names_in_block(block):
  """Return the possible real story name(s) a class block can produce."""
  base_match = re.search(r"BASE_NAME\s*=\s*['\"]([^'\"]+)['\"]", block)
  if not base_match:
    return []
  base_name = base_match.group(1)
  names = [base_name]
  year_match = re.search(r"YEAR\s*=\s*['\"]?(\d+)['\"]?", block)
  if year_match:
    names.append(f"{base_name}_{year_match.group(1)}")
  return names


def resolve_story_url(chromium_src, story):
  """Find the recorded URL for a page_sets story by its (real) story name."""
  page_sets_dir = chromium_src / "tools" / "perf" / "page_sets"
  if not page_sets_dir.is_dir():
    raise LookupError(f"page_sets directory not found: {page_sets_dir}")

  matches = []
  for py_file in page_sets_dir.rglob("*.py"):
    try:
      text = py_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
      continue
    for block in _iter_class_blocks(text):
      if story not in _story_names_in_block(block):
        continue
      # URL can be a single quoted literal or a parenthesized, possibly
      # multi-line, implicit string concatenation.
      url_match = re.search(r"URL\s*=\s*(\(.*?\)|['\"][^'\"]*['\"])", block, re.DOTALL)
      if url_match:
        parts = re.findall(r"['\"]([^'\"]*)['\"]", url_match.group(1))
        matches.append(("".join(parts), py_file))

  if not matches:
    raise LookupError(
        f'No story named "{story}" found under {page_sets_dir} '
        "(checked BASE_NAME, and BASE_NAME + '_' + YEAR for classes with a "
        "YEAR, e.g. BASE_NAME='ebay_pinch' + YEAR='2018' -> 'ebay_pinch_2018')"
    )
  urls = {u for u, _ in matches}
  if len(urls) > 1:
    detail = "\n".join(f"  {u}  (in {f})" for u, f in matches)
    raise LookupError(f'Story "{story}" matched multiple different URLs:\n{detail}')
  return matches[0]


def resolve_story_archive(chromium_src, story, benchmark=None):
  """Find which .wprgo archive contains a page_sets story, by BASE_NAME.

  The same story often exists in more than one benchmark's data index (e.g.
  rendering_desktop.json vs rendering_mobile.json for the same story, with
  different recorded archives per viewport). Default to a "desktop" index
  when that happens, since this tool targets desktop Chrome; --benchmark
  overrides that.
  """
  data_dir = chromium_src / "tools" / "perf" / "page_sets" / "data"
  if not data_dir.is_dir():
    raise LookupError(f"page_sets data directory not found: {data_dir}")

  if benchmark:
    json_files = [data_dir / f"{benchmark}.json"]
    if not json_files[0].exists():
      raise LookupError(f"No such benchmark data index: {json_files[0]}")
  else:
    json_files = sorted(data_dir.glob("*.json"))

  matches = []
  for json_file in json_files:
    try:
      index = json.loads(json_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
      continue
    entry = index.get("archives", {}).get(story)
    if not entry:
      continue
    filename = entry.get("DEFAULT") or next(iter(entry.values()))
    matches.append((data_dir / filename, json_file))

  if not matches:
    where = f"benchmark {benchmark}" if benchmark else f"any *.json under {data_dir}"
    raise LookupError(f'No wprgo archive mapping for story "{story}" found in {where}')

  archives = {a for a, _ in matches}
  if len(archives) == 1:
    return matches[0]

  if not benchmark:
    desktop_matches = [(a, f) for a, f in matches if "desktop" in f.stem]
    if len({a for a, _ in desktop_matches}) == 1:
      return desktop_matches[0]

  detail = "\n".join(f"  {a}  (--benchmark {f.stem})" for a, f in matches)
  raise LookupError(
      f'Story "{story}" exists in multiple benchmarks; pick one with '
      f"--benchmark:\n{detail}"
  )


def print_banner(archive_path, story, url, ports, chrome, chrome_args, user_data_dir):
  lines = [
      "=" * 60,
      "WPR replay is ready",
      f"  archive     : {archive_path}",
  ]
  if story:
    lines.append(f"  story       : {story}")
  lines += [
      f"  url         : {url}",
      f"  http proxy  : 127.0.0.1:{ports['http']}",
      f"  https proxy : 127.0.0.1:{ports['https']}",
      f"  chrome      : {chrome}",
      f"  chrome args : {chrome_args}",
      f"  user-data   : {user_data_dir}",
      "",
      "notes:",
      "  - Only URLs/domains captured in this archive will load; everything",
      "    else will fail to load in the opened Chrome window (expected).",
      "  - Press Enter in this terminal to close Chrome and stop the replay.",
      "=" * 60,
  ]
  print("\n".join(lines))


def main():
  parser = argparse.ArgumentParser(
      description=__doc__,
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog=EPILOG,
  )
  parser.add_argument(
      "--archive",
      help="Path to the .wprgo archive to replay "
      "(auto-resolved from --story if omitted)",
  )
  parser.add_argument(
      "--url",
      help="Exact URL recorded in the archive, scheme/host/path must match "
      "(auto-resolved from --story if omitted)",
  )
  parser.add_argument(
      "--story",
      help="page_sets story BASE_NAME, e.g. microsoft_worker_fountains. "
      "Used to auto-resolve --url and --archive when they are omitted.",
  )
  parser.add_argument(
      "--chromium-src",
      default=str(DEFAULT_CHROMIUM_SRC),
      help="Path to the chromium/src checkout, used to resolve --story "
      "(default: %(default)s)",
  )
  parser.add_argument(
      "--benchmark",
      default=None,
      help="Benchmark data index to use when resolving --story's archive, "
      "e.g. rendering_desktop (matches tools/perf/page_sets/data/"
      "<benchmark>.json). Only needed if the story exists in more than one "
      "benchmark's index; default picks the desktop one automatically.",
  )
  parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Path to chrome.exe")
  parser.add_argument(
      "--args", default=DEFAULT_ARGS, help="Extra chrome flags, space separated"
  )
  parser.add_argument(
      "--wpr-dir",
      default=None,
      help="Path to the third_party/webpagereplay checkout "
      "(default: <chromium-src>/third_party/webpagereplay)",
  )
  parser.add_argument("--http-port", type=int, default=0)
  parser.add_argument("--https-port", type=int, default=0)
  parser.add_argument(
      "--user-data-dir",
      default=None,
      help="Chrome profile dir (default: a fresh temp dir)",
  )
  args = parser.parse_args()

  if not args.story and not args.url:
    parser.error("must pass --url, or --story to resolve it automatically")
  if not args.story and not args.archive:
    parser.error("must pass --archive, or --story to resolve it automatically")

  chromium_src = Path(args.chromium_src).resolve()

  url = args.url
  archive_arg = args.archive

  if args.story:
    if not url:
      try:
        url, url_source = resolve_story_url(chromium_src, args.story)
      except LookupError as e:
        parser.error(str(e))
      print(f"[story] --story={args.story} -> --url={url}  (from {url_source})")
    if not archive_arg:
      try:
        archive_arg, archive_source = resolve_story_archive(
            chromium_src, args.story, benchmark=args.benchmark
        )
      except LookupError as e:
        parser.error(str(e))
      print(
          f"[story] --story={args.story} -> --archive={archive_arg}  "
          f"(from {archive_source})"
      )

  archive_path = Path(archive_arg).resolve()
  if not archive_path.exists():
    parser.error(
        f"wprgo archive not found: {archive_path}\n"
        "It's listed in page_sets/data but not present on disk. Fetch it "
        "first, e.g. by running the real tools/perf/run_benchmark once so it "
        "downloads the missing archive, or copy the .wprgo file in manually."
    )

  wpr_dir = (
      Path(args.wpr_dir).resolve()
      if args.wpr_dir
      else (chromium_src / "third_party" / "webpagereplay")
  )
  if not (wpr_dir / "scripts" / "run_wpr.py").exists():
    parser.error(
        f"scripts\\run_wpr.py not found under {wpr_dir}.\n"
        f"Sync it first: cd {chromium_src} && gclient sync"
    )

  user_data_dir = args.user_data_dir or tempfile.mkdtemp(prefix="wpr_chrome_profile_")

  server = WprServer(wpr_dir, archive_path, args.http_port, args.https_port)
  ports = server.start()

  # Chrome always speaks CONNECT to the "https=" proxy; WPR's https port
  # expects a raw TLS ClientHello instead. See ConnectUnwrapProxy.
  https_shim = ConnectUnwrapProxy(ports["https"])
  https_shim.start()

  chrome_cmd = (
      [
          args.chrome,
          f'--proxy-server=http=127.0.0.1:{ports["http"]};https=127.0.0.1:{https_shim.port}',
          # wpr terminates HTTPS with its own cert (wpr_cert.pem); ignore the
          # resulting cert errors instead of installing it as a trusted root.
          "--ignore-certificate-errors",
          f"--user-data-dir={user_data_dir}",
      ]
      + shlex.split(args.args)
      + [url]
  )

  print_banner(archive_path, args.story, url, ports, args.chrome, args.args, user_data_dir)
  chrome_proc = subprocess.Popen(chrome_cmd)

  try:
    input("Press Enter here to stop Chrome and the WPR replay...\n")
  except KeyboardInterrupt:
    pass
  finally:
    if chrome_proc.poll() is None:
      chrome_proc.terminate()
    https_shim.stop()
    server.stop()
    print("Stopped.")


if __name__ == "__main__":
  main()
