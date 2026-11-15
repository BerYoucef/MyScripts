#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🦀 setup_rust.py v2.8 - Install Rust (official MSVC target) on Windows without Visual Studio

Usage:
    python setup_rust.py                  full install
    python setup_rust.py --with-llvm      also install LLVM (clang-cl) for crates that compile C/C++
    python setup_rust.py --no-llvm        never ask about LLVM
    python setup_rust.py --check          verify the install and run a build test only
    python setup_rust.py --update         update Rust and re-download the CRT/SDK (an existing LLVM is kept; delete ~/.llvm to reinstall it)
    python setup_rust.py --dry-run        show what would be done, change nothing
    python setup_rust.py --uninstall      remove what this script installed (xwin, LLVM, config block)
    python setup_rust.py --accept-license accept the Microsoft CRT/SDK license non-interactively
    python setup_rust.py --set-default    make the MSVC toolchain the rustup default
    python setup_rust.py --yes            accept default answers (does NOT accept licenses)
    python setup_rust.py --no-color       no colors
"""
import argparse
import ctypes
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

if sys.version_info < (3, 8):
    sys.exit("Python 3.8+ required")

# ───────────────────────── Settings ─────────────────────────
XWIN_VERSION = "0.10.0"         # 0.6.x fails on the current VS catalog; if this tag is missing (404) the latest is used
MIN_FREE_GB = 5
LLVM_EXTRA_GB = 4              # extra space needed when LLVM is installed (archive + extracted files)
UA = "setup-rust-script"

HOME = os.path.expanduser("~")
XWIN_DIR = os.path.join(HOME, ".xwin")
LLVM_DIR = os.path.join(HOME, ".llvm")
CARGO_HOME = os.environ.get("CARGO_HOME", os.path.join(HOME, ".cargo"))
RUSTUP_HOME = os.environ.get("RUSTUP_HOME", os.path.join(HOME, ".rustup"))
TMP = os.path.join(tempfile.gettempdir(), "rust_setup")
LOG = os.path.join(TMP, "setup.log")
TOOLCHAIN = "stable-x86_64-pc-windows-msvc"
TARGET = "x86_64-pc-windows-msvc"
CARGO = ["cargo", f"+{TOOLCHAIN}"]
BEGIN, END = "# --- xwin begin ---", "# --- xwin end ---"
HOSTS = ["static.rust-lang.org", "api.github.com", "github.com", "aka.ms", "download.visualstudio.microsoft.com"]

# Linker setups tried in order by the self-test until one links successfully.
# (use absolute rust-lld path, pass -Clinker-flavor=lld-link)
VARIANTS = [(False, True), (False, False), (True, True), (True, False)]
STATE = {"variant": 0}
EXTRA = []   # (label, status) rows shown in the final summary


def describe_variant(i):
    absolute, flavor = VARIANTS[i]
    return f"{'abs path' if absolute else 'rust-lld'}, flavor {'lld-link' if flavor else 'auto'}"


# ───────────────────────── UI ─────────────────────────
class C:
    R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
    RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"
    BLU = "\033[94m"; MAG = "\033[95m"; CYN = "\033[96m"; ORG = "\033[38;5;208m"


def init_console(no_color):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if os.name == "nt":
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        m = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(m)):
            k.SetConsoleMode(h, m.value | 4)  # enable ANSI colors
    if no_color or not sys.stdout.isatty():
        for k_ in [a for a in vars(C) if a.isupper()]:
            setattr(C, k_, "")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def info(m): print(f"  {C.BLU}ℹ️  {m}{C.R}", flush=True); log(m)
def ok(m): print(f"  {C.GRN}✅ {m}{C.R}", flush=True); log("OK " + m)
def warn(m): print(f"  {C.YEL}⚠️  {m}{C.R}", flush=True); log("WARN " + m)
def err(m): print(f"  {C.RED}❌ {m}{C.R}", flush=True); log("ERR " + m)


def banner():
    print(f"""{C.ORG}
      _~^~^~_
  \\) /  o o  \\ (/      {C.B}{C.CYN}Rust without Visual Studio  v2.8{C.R}{C.ORG}
    '_   -   _'        {C.D}MSVC target + rust-lld + xwin{C.R}{C.ORG}
    / '-----' \\
{C.R}""")


def step_header(n, total, title):
    line = "─" * 50
    print(f"\n{C.MAG}┌{line}┐{C.R}")
    print(f"{C.MAG}│{C.R} {C.B}[{n}/{total}] {title}{C.R}")
    print(f"{C.MAG}└{line}┘{C.R}", flush=True)


def ask(q, default=True, assume_yes=False):
    if assume_yes:
        return default
    d = "Y/n" if default else "y/N"
    a = input(f"  {C.CYN}❓ {q} [{d}] {C.R}").strip().lower()
    return default if not a else a in ("y", "yes")


class Spinner:
    def __init__(self, text):
        self.text, self.stop_ev = text, threading.Event()

    def __enter__(self):
        def run():
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i, t0 = 0, time.time()
            while not self.stop_ev.is_set():
                print(f"\r  {C.CYN}{frames[i % 10]}{C.R} {self.text} {C.D}{int(time.time() - t0)}s{C.R}  ", end="", flush=True)
                i += 1
                time.sleep(0.1)
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop_ev.set()
        self.t.join()
        print("\r" + " " * 78 + "\r", end="")


def draw_bar(done, total, t0, base=0):
    w = 28
    el = max(time.time() - t0, 1e-6)
    sp = (done - base) / el
    if total:
        f = int(w * done / total)
        eta = int((total - done) / sp) if sp > 0 else 0
        line = (f"  {C.CYN}{'█' * f}{C.D}{'░' * (w - f)}{C.R} {done * 100 // total:3d}%  "
                f"{done / 1e6:7.1f}/{total / 1e6:.1f}MB  {sp / 1e6:5.1f}MB/s  ETA {eta}s")
    else:
        line = f"  {C.CYN}{done / 1e6:7.1f}MB{C.R}  {sp / 1e6:5.1f}MB/s"
    print("\r" + line + "   ", end="", flush=True)


# ───────────────────────── Network / file helpers ─────────────────────────
def http_get(url, headers=None, timeout=30):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def gh_json(url):
    h = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    try:
        with http_get(url, h) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RuntimeError("GitHub API rate limit reached. Wait a while, or set the GITHUB_TOKEN "
                               "environment variable (any token, no scopes needed) and run again")
        raise


def download(url, dest, retries=4):
    """Download with retries and resume (.part file + HTTP Range)."""
    log(f"download {url}")
    part = dest + ".part"
    for attempt in range(1, retries + 1):
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            hdr = {"Range": f"bytes={have}-"} if have else {}
            with http_get(url, hdr) as r:
                length = int(r.headers.get("Content-Length") or 0)
                if have and getattr(r, "status", 200) == 206:
                    cr = re.match(r"\s*bytes\s+(\d+)-", r.headers.get("Content-Range") or "")
                    if not cr or int(cr.group(1)) != have:
                        os.remove(part)
                        raise OSError("server resumed from an unexpected position, restarting the download")
                    mode, total = "ab", have + length
                else:
                    mode, total, have = "wb", length, 0
                if have:
                    info(f"Resuming from {have / 1e6:.1f}MB")
                done, base, t0, last = have, have, time.time(), 0
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 0.1:
                            draw_bar(done, total, t0, base)
                            last = time.time()
                draw_bar(done, total, t0, base)
                print()
            if total and done < total:
                raise OSError(f"incomplete download ({done}/{total} bytes)")
            os.replace(part, dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and os.path.exists(part):
                m = re.search(r"/(\d+)\s*$", e.headers.get("Content-Range") or "")
                if m and os.path.getsize(part) == int(m.group(1)):   # .part really is complete
                    os.replace(part, dest)
                    return
                warn("The partial file does not match the size on the server, restarting the download")
                os.remove(part)
                if attempt == retries:
                    raise
                continue
            print()
            warn(f"HTTP {e.code} (attempt {attempt}/{retries})")
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            print()
            warn(f"Download interrupted (attempt {attempt}/{retries}): {e}")
            if attempt == retries:
                raise
            time.sleep(2 * attempt)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify(path, expected):
    """Integrity check only: the hash comes from the same publisher, so it catches corruption, not tampering."""
    if not expected:
        warn("No published checksum available, skipping integrity check")
        return
    expected = expected.split(":")[-1].lower()
    if sha256_of(path) != expected:
        os.remove(path)
        raise RuntimeError("SHA256 mismatch: the file is corrupted (deleted, run again)")
    ok("Checksum matches (integrity check) 🔎")


def inside(root, path):
    r = os.path.normcase(os.path.realpath(root))
    p = os.path.normcase(os.path.realpath(path))
    return p == r or p.startswith(r + os.sep)


def tar_extract(t, m, out):
    if not inside(out, os.path.join(out, m.name)):
        raise tarfile.TarError(f"unsafe path in archive: {m.name}")
    if not (m.isreg() or m.isdir()):
        raise tarfile.TarError(f"unsupported entry type in archive: {m.name}")
    try:
        t.extract(m, out, filter="data")      # Python 3.12+ safe extraction
    except TypeError:
        t.extract(m, out)


def broadcast_env():
    """Tell running programs that the environment changed. Never fatal: the registry is already updated."""
    try:
        from ctypes import wintypes
        f = ctypes.WinDLL("user32", use_last_error=True).SendMessageTimeoutW
        f.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                      wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        f.restype = wintypes.LPARAM
        f(wintypes.HWND(0xFFFF), 0x1A, 0, "Environment", 2, 5000, None)   # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG
    except Exception as e:
        log(f"broadcast_env failed: {e!r}")


def user_path(action, folder):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
    try:
        cur, _ = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        cur = ""
    norm = lambda s: os.path.normcase(os.path.normpath(os.path.expandvars(s)))
    parts = [p for p in cur.split(";") if p]
    has = any(norm(p) == norm(folder) for p in parts)
    if action == "add" and not has:
        parts.append(folder)
    elif action == "remove" and has:
        parts = [p for p in parts if norm(p) != norm(folder)]
    else:
        winreg.CloseKey(key)
        return False
    winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(parts))
    winreg.CloseKey(key)
    broadcast_env()
    return True


def session_path(folder):
    norm = lambda s: os.path.normcase(os.path.normpath(s))
    if norm(folder) not in (norm(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p):
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def fwd(p):
    return p.replace("\\", "/")


# ───────────────────────── Paths / state ─────────────────────────
def lld_path():
    try:
        root = subprocess.check_output(["rustc", f"+{TOOLCHAIN}", "--print", "sysroot"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        root = os.path.join(RUSTUP_HOME, "toolchains", TOOLCHAIN)
    return os.path.join(root, "lib", "rustlib", TARGET, "bin", "rust-lld.exe")


def xwin_markers(root=None):
    root = root or XWIN_DIR
    return [
        os.path.join(root, "crt", "lib", "x86_64", "libcmt.lib"),
        os.path.join(root, "sdk", "lib", "um", "x86_64", "kernel32.lib"),
        os.path.join(root, "sdk", "lib", "ucrt", "x86_64", "ucrt.lib"),
        os.path.join(root, "sdk", "include", "um", "windows.h"),
    ]


LLVM_REQUIRED = ["clang-cl.exe", "llvm-lib.exe"]


def xwin_complete(root=None):
    return all(os.path.exists(m) for m in xwin_markers(root))


def swap_dir(new, final):
    """Replace `final` with `new` only after `new` is complete; roll back if the swap fails."""
    old = final + ".old"
    if os.path.exists(old) and not os.path.exists(final):
        os.rename(old, final)              # recover the copy left by an interrupted swap
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(final):
        os.rename(final, old)
    try:
        os.rename(new, final)
    except OSError:
        if os.path.exists(old):
            try:
                os.rename(old, final)
            except OSError as e2:
                raise RuntimeError(f"Swap failed and the automatic restore failed too. Your previous install is "
                                   f"safe at {old}; rename it back to {final}") from e2
        raise
    shutil.rmtree(old, ignore_errors=True)


def llvm_installed():
    return all(os.path.exists(os.path.join(LLVM_DIR, "bin", b)) for b in LLVM_REQUIRED)


def cargo_config_path():
    return os.path.join(CARGO_HOME, "config.toml")


def config_has_block():
    try:
        with open(cargo_config_path(), encoding="utf-8") as f:
            return BEGIN in f.read()
    except OSError:
        return False


def env_conflicts():
    """Environment variables that silently override the rustflags/linker we write to config.toml."""
    t = TARGET.upper().replace("-", "_")
    names = ["RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", f"CARGO_TARGET_{t}_RUSTFLAGS", f"CARGO_TARGET_{t}_LINKER"]
    found = [n for n in names if os.environ.get(n)]
    for n in found:
        warn(f"Environment variable {n} is set. It overrides the library paths/linker in config.toml "
             f"and will break linking. Remove it (or move its flags into config.toml).")
    legacy = os.path.join(CARGO_HOME, "config")
    if os.path.exists(legacy):
        warn(f"Legacy Cargo config file found: {legacy} (config.toml is preferred, merge them to avoid confusion)")
    c = shutil.which("cargo")
    if c and os.path.normcase(os.path.dirname(c)) != os.path.normcase(os.path.join(CARGO_HOME, "bin")):
        warn(f"cargo found at {c}, not in {os.path.join(CARGO_HOME, 'bin')} (another Rust install may shadow rustup)")
    return found


# ───────────────────────── Steps ─────────────────────────
def preflight(args):
    if sys.platform != "win32":
        raise SystemExit("This script is for Windows only")
    os.makedirs(TMP, exist_ok=True)
    arch = platform.machine().upper()
    if arch not in ("AMD64", "X86_64"):
        raise RuntimeError(f"Unsupported CPU architecture ({arch}). This script supports x86_64 only")
    if not sys.maxsize > 2 ** 32:
        raise RuntimeError("64-bit Python required")
    ok(f"Windows x64 + Python {sys.version.split()[0]}")
    drives = {}
    for label, path in (("install (HOME)", HOME), ("downloads (TEMP)", TMP)):
        drives.setdefault(os.path.splitdrive(os.path.abspath(path))[0].upper(), []).append((label, path))
    for drive, uses in drives.items():
        free = shutil.disk_usage(uses[0][1]).free / 1e9
        need = MIN_FREE_GB if len(uses) > 1 else max(2.5, MIN_FREE_GB * 0.7)
        if getattr(args, "want_llvm", False):
            need += LLVM_EXTRA_GB
        names = " + ".join(u[0] for u in uses)
        if free < need:
            raise RuntimeError(f"Drive {drive or '?'} ({names}): only {free:.1f}GB free, need about {need:.1f}GB")
        ok(f"Drive {drive or '?'} ({names}): {free:.1f}GB free 💾")
    env_conflicts()
    if args.check:
        return
    info("Checking internet connection...")
    try:
        http_get("https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe.sha256", timeout=15).close()
        ok("Internet connection works 🌐")
    except Exception as e:
        raise RuntimeError(f"Cannot reach static.rust-lang.org: {e}")
    blocked = []
    for h in HOSTS:
        try:
            socket.create_connection((h, 443), timeout=8).close()
        except OSError:
            blocked.append(h)
    if blocked:
        warn("Could not open a direct connection to: " + ", ".join(blocked))
        warn("Fine if you use a proxy; otherwise later steps may fail (firewall/VPN/DNS?)")
    else:
        ok("All required hosts reachable")


def install_rust(args):
    session_path(os.path.join(CARGO_HOME, "bin"))
    rustup = shutil.which("rustup")
    if rustup:
        ok("rustup already installed, making sure the MSVC toolchain is present")
        subprocess.check_call([rustup, "toolchain", "install", TOOLCHAIN, "--profile", "default"])
        r = subprocess.run([rustup, "default"], capture_output=True, text=True)
        current = (r.stdout or "").strip()
        if args.set_default or r.returncode != 0 or not current or "no default" in current.lower():
            subprocess.check_call([rustup, "default", TOOLCHAIN])
            ok(f"Default toolchain set to {TOOLCHAIN}")
        else:
            info(f"Your default toolchain was left unchanged ({current.split()[0]})")
            if "msvc" not in current:
                info(f"Use the MSVC one with: cargo +{TOOLCHAIN} build   (or rerun with --set-default)")
        return
    exe = os.path.join(TMP, "rustup-init.exe")
    url = f"https://static.rust-lang.org/rustup/dist/{TARGET}/rustup-init.exe"
    download(url, exe)
    try:
        with http_get(url + ".sha256") as r:
            expected = r.read().decode().split()[0]
    except Exception:
        expected = None
    verify(exe, expected)
    subprocess.check_call([exe, "-y", "--default-host", TARGET,
                           "--default-toolchain", "stable", "--profile", "default"])
    ok("Rust installed 🦀")


def update_rust(args):
    session_path(os.path.join(CARGO_HOME, "bin"))
    rustup = shutil.which("rustup")
    if not rustup:
        raise RuntimeError("rustup not found, run the full install first")
    subprocess.check_call([rustup, "update", TOOLCHAIN])
    ok("Rust updated")


def fetch_xwin_release():
    base = "https://api.github.com/repos/Jake-Shadle/xwin/releases/"
    try:
        return gh_json(base + f"tags/{XWIN_VERSION}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise RuntimeError(f"GitHub API error {e.code} while looking up xwin {XWIN_VERSION}")
        warn(f"Version {XWIN_VERSION} not found, using the latest release")
        return gh_json(base + "latest")


def xwin_works(exe, expect=None):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    return expect is None or expect.lstrip("v") in (r.stdout + r.stderr)


def get_xwin():
    exe = os.path.join(TMP, "xwin.exe")
    vfile = exe + ".version"
    try:
        with open(vfile, encoding="utf-8") as f:
            cached = f.read().split()          # [requested, actual]
    except OSError:
        cached = []
    if os.path.exists(exe) and cached and cached[0] == XWIN_VERSION and xwin_works(exe, cached[-1]):
        info(f"Using cached xwin {cached[-1]}")
        return exe
    if os.path.exists(exe):
        info("Cached xwin is outdated or damaged, downloading again")
    rel = fetch_xwin_release()
    asset = next(a for a in rel["assets"] if TARGET in a["name"] and a["name"].endswith(".tar.gz"))
    info(f"xwin {rel['tag_name']}")
    tgz = os.path.join(TMP, asset["name"])
    download(asset["browser_download_url"], tgz)
    verify(tgz, asset.get("digest"))
    with tarfile.open(tgz) as t:
        m = next((m for m in t.getmembers() if os.path.basename(m.name) == "xwin.exe" and m.isfile()), None)
        if not m:
            raise RuntimeError("xwin.exe not found inside the archive")
        m.name = "xwin.exe"
        tar_extract(t, m, TMP)
    if not xwin_works(exe, rel["tag_name"]):
        os.remove(exe)
        raise RuntimeError("The downloaded xwin.exe does not run or reports a different version")
    with open(vfile, "w", encoding="utf-8") as f:
        f.write(f"{XWIN_VERSION}\n{rel['tag_name']}\n")
    return exe


def confirm_license(args):
    if args.accept_license:
        return
    print(f"\n  {C.YEL}📜 The CRT and Windows SDK are downloaded from Microsoft and covered by the")
    print(f"     Microsoft Visual Studio / Windows SDK license terms (see https://github.com/Jake-Shadle/xwin).{C.R}")
    if args.yes:
        raise RuntimeError("--yes never accepts licenses. Add --accept-license if you accept the terms")
    if not ask("Do you accept the Microsoft license terms?", False):
        raise RuntimeError("License not accepted, stopping")


def install_xwin(args, force=False):
    if xwin_complete() and not force:
        ok("CRT and SDK already complete")
        return
    if os.path.isdir(XWIN_DIR) and not xwin_complete():
        warn("Incomplete previous install found, it will be replaced")
    confirm_license(args)
    xwin = get_xwin()
    staging = XWIN_DIR + ".new"           # build in a staging folder, swap only on success
    shutil.rmtree(staging, ignore_errors=True)
    info("Downloading CRT + Windows SDK from Microsoft (may take several minutes ☕)")
    try:
        subprocess.check_call([xwin, "--accept-license", "--arch", "x86_64",
                               "--cache-dir", os.path.join(TMP, "xwin-cache"),
                               "splat", "--output", staging, "--disable-symlinks"])
        if not xwin_complete(staging):
            missing = [os.path.relpath(m, staging) for m in xwin_markers(staging) if not os.path.exists(m)]
            raise RuntimeError("xwin finished but essential files are missing: " + ", ".join(missing))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.isdir(XWIN_DIR):
            info("Your previous CRT/SDK was left untouched")
        raise
    swap_dir(staging, XWIN_DIR)
    ok("CRT and SDK ready 📚")


def refresh_xwin(args):
    install_xwin(args, force=True)


def resolve_link(root, m):
    rel = m.linkname if m.islnk() else os.path.join(os.path.dirname(m.name), m.linkname)
    return os.path.join(root, os.path.normpath(rel))


def is_critical(name):
    parts = name.replace("\\", "/").split("/")
    return any(p in ("bin", "lib", "include") for p in parts[:2])


def extract_llvm(arc, out):
    """Extract; links (which often fail on Windows) are replaced by copies of their targets."""
    links, failed = [], []
    with tarfile.open(arc, "r:xz") as t:
        for m in t:
            if m.issym() or m.islnk():
                links.append(m)
                continue
            try:
                tar_extract(t, m, out)
            except (OSError, tarfile.TarError) as e:
                failed.append(m.name)
                log(f"failed {m.name}: {e}")
    pending = links
    for _ in range(5):
        rest = []
        for m in pending:
            src, dst = resolve_link(out, m), os.path.join(out, os.path.normpath(m.name))
            if not (inside(out, src) and inside(out, dst)):
                log(f"unsafe link skipped: {m.name} -> {m.linkname}")
                continue
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            elif os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                rest.append(m)
        stuck = len(rest) == len(pending)      # no progress this round
        pending = rest
        if not pending or stuck:
            break
    names = [m.name for m in pending]
    log(f"llvm extract: failed={len(failed)}, unresolved links={names}")
    return failed, names


def install_llvm(args):
    if llvm_installed():
        ok("LLVM already installed")
        return
    rel = gh_json("https://api.github.com/repos/llvm/llvm-project/releases/latest")
    asset = next((a for a in rel["assets"] if a["name"].startswith("clang+llvm-")
                  and a["name"].endswith("x86_64-pc-windows-msvc.tar.xz")), None)
    if not asset:
        raise RuntimeError("No suitable Windows LLVM package found in the latest release")
    info(f"{asset['name']} ({asset.get('size', 0) / 1e6:.0f}MB)")
    arc = os.path.join(TMP, asset["name"])
    download(asset["browser_download_url"], arc)
    verify(arc, asset.get("digest"))
    tmp_out = LLVM_DIR + ".tmp"
    shutil.rmtree(tmp_out, ignore_errors=True)
    with Spinner("Extracting LLVM (this takes a while)"):
        failed, unresolved = extract_llvm(arc, tmp_out)
    critical = [n for n in failed if is_critical(n)]
    if critical:
        shutil.rmtree(tmp_out, ignore_errors=True)
        raise RuntimeError(f"{len(critical)} essential LLVM files failed to extract (e.g. {critical[0]}). "
                           "Check free disk space / antivirus, then run again")
    candidates = [os.path.join(tmp_out, n) for n in os.listdir(tmp_out)] + [tmp_out]
    top = next((c for c in candidates if os.path.isdir(c)
                and os.path.exists(os.path.join(c, "bin", "clang-cl.exe"))), None)
    if not top:
        shutil.rmtree(tmp_out, ignore_errors=True)
        raise RuntimeError("clang-cl.exe not found in the extracted archive (unexpected layout)")
    missing = [b for b in LLVM_REQUIRED if not os.path.exists(os.path.join(top, "bin", b))]
    if missing:
        shutil.rmtree(tmp_out, ignore_errors=True)
        raise RuntimeError(f"LLVM extraction incomplete, missing: {', '.join(missing)}")
    if not glob.glob(os.path.join(top, "lib", "clang", "*", "include", "stddef.h")):
        shutil.rmtree(tmp_out, ignore_errors=True)
        raise RuntimeError("clang resource headers (lib/clang/*/include) are missing, extraction incomplete")
    staging = LLVM_DIR + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.move(top, staging)
    shutil.rmtree(tmp_out, ignore_errors=True)
    swap_dir(staging, LLVM_DIR)
    if failed or unresolved:
        warn(f"{len(failed)} non-essential files skipped, {len(unresolved)} links unresolved (details in the log)")
    crit_links = [n for n in unresolved if is_critical(n)]
    if crit_links:
        warn("Unresolved links inside bin/lib/include: " + ", ".join(crit_links[:5])
             + (" ..." if len(crit_links) > 5 else ""))
        warn("The C/C++ test at the end will show whether anything you need is affected")
    user_path("add", os.path.join(LLVM_DIR, "bin"))
    ok("LLVM (clang-cl) installed ⚙️")


def build_block(variant):
    absolute, flavor = VARIANTS[variant]
    p = fwd
    linker = p(lld_path()) if absolute else "rust-lld"
    flags_list = ['  "-Clinker-flavor=lld-link",'] if flavor else []
    flags_list += [f'  "-Lnative={p(XWIN_DIR)}/crt/lib/x86_64",',
                   f'  "-Lnative={p(XWIN_DIR)}/sdk/lib/um/x86_64",',
                   f'  "-Lnative={p(XWIN_DIR)}/sdk/lib/ucrt/x86_64",']
    block = f"{BEGIN}\n[target.{TARGET}]\nlinker = \"{linker}\"\nrustflags = [\n" + "\n".join(flags_list) + "\n]\n"
    if llvm_installed():
        xw = p(XWIN_DIR)      # forward slashes: backslashes would be read as TOML escapes
        inc = [f"{xw}/crt/include", f"{xw}/sdk/include/ucrt",
               f"{xw}/sdk/include/um", f"{xw}/sdk/include/shared"]
        cflags = " ".join(f"/imsvc {i}" for i in inc)
        b = p(os.path.join(LLVM_DIR, "bin"))
        block += f"""
[env]
CC_x86_64_pc_windows_msvc = "{b}/clang-cl.exe"
CXX_x86_64_pc_windows_msvc = "{b}/clang-cl.exe"
AR_x86_64_pc_windows_msvc = "{b}/llvm-lib.exe"
CFLAGS_x86_64_pc_windows_msvc = "{cflags}"
CXXFLAGS_x86_64_pc_windows_msvc = "{cflags}"
"""
    return block + END + "\n"


def block_wellformed(text):
    """Exactly one BEGIN followed by exactly one END."""
    return (text.count(BEGIN) == 1 and text.count(END) == 1
            and text.index(BEGIN) < text.index(END))


def strip_block(text):
    """Remove our block. A damaged or duplicated block is left untouched rather than guessed at."""
    if block_wellformed(text):
        a = text.index(BEGIN)
        b = text.index(END) + len(END)
        text = text[:a] + text[b:].lstrip("\n")
    return text


def write_cargo_config(args, quiet=False):
    if not os.path.exists(lld_path()):
        warn(f"rust-lld not found at {lld_path()} (the toolchain may be incomplete)")
    path = cargo_config_path()
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        rest = strip_block(old)
        if BEGIN in rest or END in rest:
            warn("config.toml has a damaged or duplicated xwin begin/end marker; file left untouched")
            warn("Fix or delete the marker lines by hand, then run again. Block to use:")
            print(C.D + build_block(STATE["variant"]) + C.R)
            return "skipped"
        if f"[target.{TARGET}]" in rest or "[env]" in rest:
            warn("config.toml already has [target] or [env] outside this script's block")
            warn("File left untouched to avoid conflicts. Copy this block manually:")
            print(C.D + build_block(STATE["variant"]) + C.R)
            return "skipped"
        bak = path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
            info(f"Backup saved: {bak}")
        old = rest
    os.makedirs(CARGO_HOME, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write((old.rstrip() + "\n\n" if old.strip() else "") + build_block(STATE["variant"]))
    if not quiet:
        ok(f"Updated {path} 🧩")
    return "configured"


def run_quiet(cmd, cwd, text):
    with Spinner(text):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"{cmd}\n{r.stdout}\n{r.stderr}")
    return r


def linker_error(stderr):
    s = stderr.lower()
    return any(k in s for k in ("linker", "lld", "lnk", "link.exe", "cannot open", "unable to find library"))


def config_status():
    try:
        with open(cargo_config_path(), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "missing"
    if BEGIN not in text:
        return "missing"
    return "ok" if fwd(XWIN_DIR) in text[text.index(BEGIN):] else "stale"


def network_error(stderr):
    s = stderr.lower()
    return any(k in s for k in ("failed to download", "network failure", "could not resolve host",
                                "unable to update registry", "spurious network error",
                                "failed to get `cc`", "failed to query replaced source",
                                "index.crates.io", "timed out"))


def self_test(args):
    session_path(os.path.join(CARGO_HOME, "bin"))
    if llvm_installed():
        session_path(os.path.join(LLVM_DIR, "bin"))
    for m in xwin_markers():
        (ok if os.path.exists(m) else err)(os.path.relpath(m, XWIN_DIR))
    if not xwin_complete():
        raise RuntimeError("CRT/SDK files are missing, run the full install")
    if not os.path.exists(os.path.join(XWIN_DIR, "crt", "include", "vector")):
        warn("C++ standard library headers (crt/include/vector) not found; C++ crates may fail to build")
    if not shutil.which("cargo"):
        raise RuntimeError("cargo not found in PATH")
    managed = config_has_block()
    with tempfile.TemporaryDirectory() as d:
        t0 = time.time()
        r0 = run_quiet(CARGO + ["new", "hello", "--quiet"], d, "Creating test project")
        if r0.returncode != 0:
            print(r0.stderr[-1500:])
            raise RuntimeError("`cargo new` failed, so the build test could not start (details in the log)")
        proj = os.path.join(d, "hello")
        while True:
            idx = STATE["variant"]
            r = run_quiet(CARGO + ["run", "--quiet"], proj, f"Building Rust ({describe_variant(idx)})")
            if r.returncode == 0 and "Hello, world!" in r.stdout:
                break
            if managed and not args.check and linker_error(r.stderr) and idx + 1 < len(VARIANTS):
                STATE["variant"] = idx + 1
                warn(f"Linking failed, trying another linker setup: {describe_variant(idx + 1)}")
                write_cargo_config(args, quiet=True)
                continue
            print(r.stderr[-1500:])
            if args.check and managed and linker_error(r.stderr):
                warn("--check never edits your Cargo config. Run without --check to let the script try other linker setups")
            raise RuntimeError("Rust test project failed (full details in the log)")
        ok(f"Rust project builds and runs ✨ ({time.time() - t0:.1f}s)")
        EXTRA.append(("Rust build (MSVC toolchain)", "ok"))
        cs = config_status()
        EXTRA.append(("Managed Cargo config", cs))
        if cs != "ok":
            warn("The build worked, but the config block written by this script is "
                 f"{cs}: it may be succeeding through other settings (run the full install to fix)")
        if managed:
            info(f"Linker setup: {describe_variant(STATE['variant'])}")

        if llvm_installed():
            cproj = os.path.join(d, "ctest")
            os.makedirs(os.path.join(cproj, "src"))
            with open(os.path.join(cproj, "Cargo.toml"), "w") as f:
                f.write('[package]\nname="ctest"\nversion="0.1.0"\nedition="2021"\n'
                        '[build-dependencies]\ncc="1"\n')
            with open(os.path.join(cproj, "build.rs"), "w") as f:
                f.write('fn main(){\n'
                        '  cc::Build::new().file("hi.c").compile("hi");\n'
                        '  cc::Build::new().cpp(true).file("hipp.cpp").compile("hipp");\n}\n')
            with open(os.path.join(cproj, "hi.c"), "w") as f:
                f.write('#include <stdio.h>\n#include <windows.h>\nint hi(void){return 42;}\n')
            with open(os.path.join(cproj, "hipp.cpp"), "w") as f:
                f.write('#include <string>\n#include <vector>\n#include <windows.h>\n'
                        'extern "C" int hipp(){ std::vector<int> v{1,2,3}; std::string s("abc");'
                        ' return (int)(v.size() + s.size()); }\n')
            with open(os.path.join(cproj, "src", "main.rs"), "w") as f:
                f.write('extern "C"{fn hi()->i32; fn hipp()->i32;}\n'
                        'fn main(){println!("{} {}",unsafe{hi()},unsafe{hipp()});}\n')
            r = run_quiet(CARGO + ["run", "--quiet"], cproj, "Testing C and C++ builds via clang-cl")
            if r.returncode or "42 6" not in r.stdout:
                print(r.stderr[-1500:])
                if network_error(r.stderr):
                    warn("Could not download the 'cc' crate (network problem), so the C/C++ test is inconclusive. "
                         "Run --check again when online")
                    EXTRA.append(("C/C++ via clang-cl", "inconclusive"))
                elif "hipp.cpp" in r.stderr:
                    warn("C++ build failed (C++ headers/libs problem, details in the log). C and pure Rust may still work")
                    EXTRA.append(("C++ via clang-cl", "FAILED"))
                else:
                    warn("C build failed (details in the log). Pure Rust still works")
                    EXTRA.append(("C via clang-cl", "FAILED"))
            else:
                ok("C and C++ builds via clang-cl work ⚙️")
                EXTRA.append(("C/C++ via clang-cl", "ok"))


def uninstall(args):
    if not ask("This deletes xwin, LLVM, the config block and this script's downloads (not rustup). Continue?",
               False, args.yes):
        return
    for d in (XWIN_DIR, XWIN_DIR + ".new", XWIN_DIR + ".old",
              LLVM_DIR, LLVM_DIR + ".new", LLVM_DIR + ".old", LLVM_DIR + ".tmp"):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(d):
                warn(f"Could not fully remove {d} (files in use? close programs using it and run again)")
            else:
                ok(f"Removed {d} 🗑️")
    ours = ["xwin-cache", "xwin.exe", "xwin.exe.version", "rustup-init.exe"]
    for pat in ("xwin-*.tar.gz", "clang+llvm-*.tar.xz", "xwin-*.tar.gz.part",
                "clang+llvm-*.tar.xz.part", "rustup-init.exe.part"):
        ours += [os.path.basename(x) for x in glob.glob(os.path.join(TMP, pat))]
    stuck = []
    for n in ours:
        f = os.path.join(TMP, n)
        try:
            if os.path.isdir(f):
                shutil.rmtree(f, ignore_errors=True)
            elif os.path.exists(f):
                os.remove(f)
        except OSError:
            pass
        if os.path.exists(f):
            stuck.append(f)
    if stuck:
        warn(f"Could not remove {len(stuck)} download/cache item(s), e.g. {stuck[0]}")
    else:
        ok("Removed the downloads and cache created by this script")
    user_path("remove", os.path.join(LLVM_DIR, "bin"))
    path = cargo_config_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            t = f.read()
        if BEGIN in t or END in t:
            new = strip_block(t)
            if BEGIN in new or END in new:
                warn("config.toml has a damaged or duplicated xwin marker; remove the block by hand")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new.rstrip() + "\n")
                ok("Removed the xwin block from config.toml")
    print(f"  {C.BLU}ℹ️  To remove Rust itself: rustup self uninstall{C.R}")
    for leftover in (LOG,):
        try:
            os.remove(leftover)
        except OSError:
            pass
    try:
        os.rmdir(TMP)                      # only succeeds if nothing else is inside
    except OSError:
        pass


def dry_run(args, want_llvm):
    def row(label, done):
        print(f"  {C.GRN + '✔ already done' if done else C.YEL + '➜ will run    '}{C.R}  {label}")
    print(f"{C.B}🧪 Dry run: nothing will be changed{C.R}\n")
    row("Install Rust (rustup + MSVC toolchain)", shutil.which("rustup") is not None)
    row(f"Download CRT + Windows SDK into {XWIN_DIR} (~1.5GB, asks for license)", xwin_complete())
    if want_llvm:
        row(f"Install LLVM / clang-cl into {LLVM_DIR} (~500MB+)", llvm_installed())
    row(f"Add a managed block to {cargo_config_path()}", config_has_block())
    row("Build Rust (and C/C++ if LLVM) test projects to verify everything", False)
    print()
    env_conflicts()


# ───────────────────────── Main ─────────────────────────
def main():
    ap = argparse.ArgumentParser(
        prog="setup_rust.py",
        description="Install Rust (official MSVC target) on Windows without Visual Studio, "
                    "using xwin for the CRT/Windows SDK and optionally LLVM (clang-cl) for C/C++ crates.",
        epilog="examples:\n"
               "  python setup_rust.py --dry-run                 see what would be done (changes nothing)\n"
               "  python setup_rust.py --with-llvm               full install including C/C++ support\n"
               "  python setup_rust.py --no-llvm                 full install, Rust only\n"
               "  python setup_rust.py --check                   verify an existing install\n"
               "  python setup_rust.py --update                  update Rust and the CRT/SDK\n"
               "  python setup_rust.py --uninstall               remove what this script installed\n"
               "\n"
               "log file: " + LOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-llvm", action="store_true",
                    help="also install LLVM (clang-cl, ~500MB download, ~3GB on disk) for crates that compile C/C++")
    ap.add_argument("--no-llvm", action="store_true",
                    help="never install LLVM and do not ask about it")
    ap.add_argument("--check", action="store_true",
                    help="verify the install and run a build test only; never edits your Cargo config")
    ap.add_argument("--update", action="store_true",
                    help="update Rust and re-download the CRT/SDK (an existing LLVM is kept; delete ~/.llvm to reinstall it)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be done; no downloads, no files or folders created")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove xwin, LLVM, downloads and the config block this script added (Rust itself stays)")
    ap.add_argument("--accept-license", action="store_true",
                    help="accept the Microsoft CRT/Windows SDK license without asking")
    ap.add_argument("--set-default", action="store_true",
                    help="make the MSVC toolchain the rustup default (otherwise an existing default is kept)")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="accept the default answer to every question (never accepts the license)")
    ap.add_argument("--no-color", action="store_true",
                    help="disable colored output")
    args = ap.parse_args()

    init_console(args.no_color)
    banner()

    if args.uninstall:
        uninstall(args)
        return

    want_llvm = args.with_llvm or llvm_installed()
    if not want_llvm and not (args.no_llvm or args.check or args.yes or args.dry_run):
        want_llvm = ask("Also install LLVM (clang-cl, ~500MB+)? Needed by crates that compile C/C++ code", True)
    args.want_llvm = want_llvm

    if args.dry_run:
        dry_run(args, want_llvm)
        return

    os.makedirs(TMP, exist_ok=True)     # only now: --dry-run and --uninstall create nothing
    log("=== start ===")

    if args.check:
        steps = [("System check", preflight), ("Verify install", self_test)]
    elif args.update:
        steps = [("System check", preflight), ("Update Rust", update_rust),
                 ("Refresh CRT + Windows SDK", refresh_xwin)]
        if want_llvm:
            steps.append(("Install LLVM / clang-cl", install_llvm))
        steps += [("Configure Cargo", write_cargo_config), ("Verify install", self_test)]
    else:
        steps = [("System check", preflight), ("Install Rust", install_rust),
                 ("Install CRT + Windows SDK (xwin)", install_xwin)]
        if want_llvm:
            steps.append(("Install LLVM / clang-cl", install_llvm))
        steps += [("Configure Cargo", write_cargo_config), ("Verify install", self_test)]

    results = []
    for i, (title, fn) in enumerate(steps, 1):
        step_header(i, len(steps), title)
        t0 = time.time()
        try:
            res = fn(args)
            results.append((title, "skip" if res == "skipped" else "ok", time.time() - t0))
        except KeyboardInterrupt:
            err("Interrupted by user (re-run to resume, downloads continue where they stopped)")
            sys.exit(130)
        except Exception as e:
            err(str(e))
            log(f"step failed: {title}: {e!r}")
            results.append((title, "fail", time.time() - t0))
            break

    print(f"\n{C.B}📊 Summary{C.R}")
    for title, st, sec in results:
        icon = {"ok": f"{C.GRN}✔{C.R}", "skip": f"{C.YEL}⚠{C.R}"}.get(st, f"{C.RED}✘{C.R}")
        note = " (skipped)" if st == "skip" else ""
        print(f"  {icon} {title:<36}{C.D}{sec:6.1f}s{note}{C.R}")
    for label, st in EXTRA:
        icon = f"{C.GRN}✔{C.R}" if st == "ok" else (f"{C.RED}✘{C.R}" if st == "FAILED" else f"{C.YEL}⚠{C.R}")
        print(f"  {icon} {label:<36}{C.D}{st}{C.R}")
    print(f"  {C.D}📝 Log: {LOG}{C.R}")

    if results and all(r[1] in ("ok", "skip") for r in results) and len(results) == len(steps):
        print(f"\n{C.GRN}{C.B}🎉 All set!{C.R} Open a new terminal and try:")
        print(f"   {C.CYN}cargo new hello && cd hello && cargo run{C.R}")
        if any(r[1] == "skip" for r in results):
            print(f"   {C.YEL}⚠️  Your Cargo config was not modified: add the block shown above by hand{C.R}")
        if any(st != "ok" for _, st in EXTRA):
            print(f"   {C.YEL}⚠️  Some checks need attention (see the ⚠/✘ rows above){C.R}")
        if not want_llvm:
            print(f"   {C.D}💡 For C/C++ crates run: python setup_rust.py --with-llvm{C.R}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
