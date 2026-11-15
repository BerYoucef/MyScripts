#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vc_redist.py - companion of setup_rust.py (keep both files in the same folder).

Makes sure the Microsoft Visual C++ Redistributable (x64) is present. Programs built from C/C++ need
msvcp140.dll / vcruntime140.dll to RUN; building them does not need it. A clean Windows may lack them.

What it does:
  1. checks System32 for the DLLs (nothing happens if they are there)
  2. downloads the official installer from aka.ms
  3. verifies Microsoft's digital signature (Authenticode) before running anything
  4. installs it silently; Windows will show a permission (UAC) prompt, because it installs system-wide

It never decides to install by itself: setup_rust.py asks for consent first.
"""
import os
import subprocess

URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
DLLS = ("msvcp140.dll", "vcruntime140.dll")
EXE_NAME = "vc_redist.x64.exe"

# installer exit codes: code -> (status, meaning)
CODES = {
    0: ("installed", "installed"),
    3010: ("reboot", "installed, a restart is needed"),
    1641: ("reboot", "installed, a restart is needed"),
    1638: ("installed", "a newer version is already installed"),
}


def system32():
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")


def is_installed():
    s = system32()
    return all(os.path.exists(os.path.join(s, d)) for d in DLLS)


def _run_ps(command, timeout):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _q(path):
    return path.replace("'", "''")      # PowerShell single-quote escaping


def signature_ok(path):
    """True only if the file has a valid Authenticode signature issued to Microsoft Corporation."""
    cmd = (f"$s = Get-AuthenticodeSignature -LiteralPath '{_q(path)}'; "
           "\"$($s.Status)|$($s.SignerCertificate.Subject)\"")
    try:
        out = (_run_ps(cmd, 60).stdout or "").strip().splitlines()
    except Exception:
        return False
    line = out[-1] if out else ""
    status, _, subject = line.partition("|")
    return status.strip() == "Valid" and "O=Microsoft Corporation" in subject


def run_installer(path):
    """Run the installer elevated and wait. Returns the exit code, or None if the user declined the prompt."""
    cmd = (f"try {{ $p = Start-Process -FilePath '{_q(path)}' "
           "-ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait -PassThru; $p.ExitCode } "
           "catch { 'CANCELLED' }")
    out = (_run_ps(cmd, 900).stdout or "").strip().splitlines()
    last = out[-1].strip() if out else ""
    return int(last) if last.lstrip("-").isdigit() else None


def ensure(download, info, ok, warn, log, tmp_dir):
    """Returns: present | installed | reboot | cancelled | failed. Call only after the user consented."""
    if is_installed():
        return "present"
    exe = os.path.join(tmp_dir, EXE_NAME)
    info("Downloading the Microsoft Visual C++ Redistributable (x64)")
    try:
        download(URL, exe)
    except Exception as e:
        warn(f"Could not download the Visual C++ Redistributable: {e}")
        return "failed"
    if not signature_ok(exe):
        warn("The downloaded installer does not have a valid Microsoft signature, so it was NOT run (file deleted)")
        try:
            os.remove(exe)
        except OSError:
            pass
        return "failed"
    ok("Microsoft digital signature is valid 🔏")
    info("Installing... approve the Windows permission prompt (UAC) when it appears")
    try:
        code = run_installer(exe)
    except Exception as e:
        warn(f"Could not start the installer: {e}")
        return "failed"
    log(f"vc_redist exit code: {code}")
    if code is None:
        warn("The permission prompt was declined or the installer could not start")
        return "cancelled"
    status, meaning = CODES.get(code, (None, None))
    if status is None:
        warn(f"The installer exited with code {code}. You can install it by hand: {URL}")
        return "failed"
    if not is_installed():
        warn(f"The installer reported success ({meaning}) but the DLLs are not in System32 yet. "
             "A restart may be needed")
        return "reboot"
    ok(f"Visual C++ runtime: {meaning} 📦")
    return status
