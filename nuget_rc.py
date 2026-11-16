#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nuget_rc.py - companion of setup_rust.py (keep both files in the same folder).

Installs the Windows SDK command-line tools that xwin does not ship (rc.exe, rcdll.dll, mt.exe, ...).
Tauri and other crates that embed icons / version info / manifests need rc.exe to BUILD.

What it does:
  1. checks ~/.winsdk-tools/bin for rc.exe and rcdll.dll (nothing happens if they are there)
  2. downloads the official "Microsoft.Windows.SDK.BuildTools" package from nuget.org (a plain zip)
  3. extracts only the x64 tools into a staging folder
  4. verifies Microsoft's digital signature (Authenticode) on rc.exe before keeping anything
  5. swaps the staging folder in, keeping the previous install if anything fails

It never decides to install by itself: setup_rust.py asks for consent first.
It does not touch PATH: setup_rust.py does that (user PATH, same as for LLVM).
"""
import json
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile

PACKAGE = "microsoft.windows.sdk.buildtools"
INDEX_URL = f"https://api.nuget.org/v3-flatcontainer/{PACKAGE}/index.json"
PKG_URL = "https://api.nuget.org/v3-flatcontainer/{p}/{v}/{p}.{v}.nupkg"
PKG_NAME = "buildtools.nupkg"
REQUIRED = ("rc.exe", "rcdll.dll")
DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".winsdk-tools")


def bin_dir(root=None):
    return os.path.join(root or DEFAULT_ROOT, "bin")


def is_installed(root=None):
    b = bin_dir(root)
    return all(os.path.exists(os.path.join(b, f)) for f in REQUIRED)


def installed_version(root=None):
    try:
        with open(os.path.join(root or DEFAULT_ROOT, "version.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def latest_version():
    req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "setup-rust-script"})
    with urllib.request.urlopen(req, timeout=30) as r:
        versions = json.load(r).get("versions", [])
    stable = [v for v in versions if "-" not in v]      # skip previews
    if not stable:
        raise RuntimeError("no stable version found on nuget.org")
    return max(stable, key=_vkey)


def _run_ps(command, timeout):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def signature_ok(path):
    """True only if the file has a valid Authenticode signature issued to Microsoft Corporation."""
    p = path.replace("'", "''")
    cmd = (f"$s = Get-AuthenticodeSignature -LiteralPath '{p}'; "
           "\"$($s.Status)|$($s.SignerCertificate.Subject)\"")
    try:
        out = (_run_ps(cmd, 60).stdout or "").strip().splitlines()
    except Exception:
        return False
    status, _, subject = (out[-1] if out else "").partition("|")
    return status.strip() == "Valid" and "O=Microsoft Corporation" in subject


def _x64_prefix(names):
    """Highest 'bin/<version>/x64/' folder in the package (fallback: 'bin/x64/')."""
    found = {}
    for n in names:
        m = re.match(r"^(bin/([^/]+)/x64/)", n, re.I)
        if m:
            found[m.group(1)] = m.group(2)
    if found:
        return max(found, key=lambda k: _vkey(found[k]))
    if any(n.lower().startswith("bin/x64/") for n in names):
        return "bin/x64/"
    raise RuntimeError("unexpected package layout: no bin/<version>/x64/ folder")


def _extract(pkg, out_dir):
    """Extract only the x64 tools, flattened into out_dir. Returns the number of files."""
    os.makedirs(out_dir, exist_ok=True)
    real_out = os.path.realpath(out_dir)
    count = 0
    with zipfile.ZipFile(pkg) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"corrupted entry in the package: {bad}")
        prefix = _x64_prefix(z.namelist())
        for m in z.infolist():
            if m.is_dir() or not m.filename.lower().startswith(prefix.lower()):
                continue
            rel = m.filename[len(prefix):]
            dest = os.path.realpath(os.path.join(out_dir, rel))
            if not dest.startswith(real_out + os.sep):          # zip-slip guard
                raise RuntimeError(f"unsafe path in package: {m.filename}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(m) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def _swap(new, final):
    """Replace `final` with `new` only when `new` is complete; roll back if the swap fails."""
    old = final + ".old"
    if os.path.exists(old) and not os.path.exists(final):
        os.rename(old, final)
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(final):
        os.rename(final, old)
    try:
        os.rename(new, final)
    except OSError:
        if os.path.exists(old):
            os.rename(old, final)
        raise
    shutil.rmtree(old, ignore_errors=True)


def test(root=None, tmp_dir=None):
    """Compile a tiny .rc file with the installed rc.exe. True if it works."""
    import tempfile
    rc = os.path.join(bin_dir(root), "rc.exe")
    if not os.path.exists(rc):
        return False
    with tempfile.TemporaryDirectory(dir=tmp_dir) as d:
        src, out = os.path.join(d, "t.rc"), os.path.join(d, "t.res")
        with open(src, "w", encoding="ascii") as f:
            f.write('1 VERSIONINFO\nFILEVERSION 1,0,0,0\nBEGIN\nBLOCK "StringFileInfo"\nBEGIN\n'
                    'BLOCK "040904B0"\nBEGIN\nVALUE "FileDescription", "test"\nEND\nEND\nEND\n')
        try:
            r = subprocess.run([rc, "/nologo", "/fo", out, src], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
        except Exception:
            return False
        return r.returncode == 0 and os.path.exists(out)


def ensure(download, info, ok, warn, log, tmp_dir, root=None):
    """Returns: present | installed | failed. Call only after the user consented."""
    root = root or DEFAULT_ROOT
    if is_installed(root):
        return "present"
    pkg = os.path.join(tmp_dir, PKG_NAME)
    try:
        version = latest_version()
        info(f"Downloading Windows SDK build tools ({PACKAGE} {version}) from nuget.org")
        download(PKG_URL.format(p=PACKAGE, v=version), pkg)
    except Exception as e:
        warn(f"Could not download the Windows SDK build tools: {e}")
        return "failed"
    stage = root + ".new"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        count = _extract(pkg, os.path.join(stage, "bin"))
        missing = [f for f in REQUIRED if not os.path.exists(os.path.join(stage, "bin", f))]
        if missing:
            raise RuntimeError("the package does not contain " + ", ".join(missing))
        if not signature_ok(os.path.join(stage, "bin", "rc.exe")):
            raise RuntimeError("rc.exe does not have a valid Microsoft signature, so it was NOT kept")
        with open(os.path.join(stage, "version.txt"), "w", encoding="utf-8") as f:
            f.write(version + "\n")
        ok("Microsoft digital signature is valid 🔏")
        log(f"nuget_rc: extracted {count} files, version {version}")
        _swap(stage, root)
    except Exception as e:
        shutil.rmtree(stage, ignore_errors=True)
        warn(f"Could not install the Windows SDK build tools: {e}")
        log(f"nuget_rc failed: {e!r}")
        return "failed"
    finally:
        for f in (pkg, pkg + ".part"):
            try:
                os.remove(f)
            except OSError:
                pass
    ok(f"Windows SDK build tools {version} installed in {bin_dir(root)} 🧰")
    return "installed"
