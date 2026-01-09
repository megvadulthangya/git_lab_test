#!/usr/bin/env python3
"""
builder.py

Single-file consolidated build tool for Manjaro package repo:
- network diagnostics
- pacman.conf injection for a custom repo
- dependency resolution using yay/pacman
- compare local/AUR/remote versions
- build packages (makepkg)
- update PKGBUILD/pkgrel and push via git SSH (auto-commit)
- upload artifacts to VPS via scp
- retention on server: keep latest + 1 previous version (delete older)

Environment variables (must be provided to the script / workflow):
- VPS_USER
- VPS_HOST
- REMOTE_DIR (optional, defaults to /var/www/repo)
- REPO_DB_NAME (optional, defaults to manjaro-awesome)
- SSH_REPO_URL (optional, defaults to git@github.com:megvadulthangya/manjaro-awesome.git)
- GH_PAT (optional)
- ADD_CUSTOM_REPO (optional) : "true" to inject custom repo into /etc/pacman.conf
- CUSTOM_REPO_NAME, CUSTOM_REPO_SERVER, CUSTOM_REPO_SIGLEVEL (optional)
"""

import os
import sys
import subprocess
import shlex
import tempfile
import shutil
import re
import time
from pathlib import Path
from typing import List, Tuple, Optional

# -------------------------
# Configuration (defaults)
# -------------------------
REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = REPO_ROOT / "built_packages"
BUILD_TRACKING = REPO_ROOT / ".buildtracking"
REMOTE_DIR = os.environ.get("REMOTE_DIR", "/var/www/repo")
REPO_DB_NAME = os.environ.get("REPO_DB_NAME", "manjaro-awesome")
SSH_REPO_URL = os.environ.get("SSH_REPO_URL", "git@github.com:megvadulthangya/manjaro-awesome.git")
VPS_USER = os.environ.get("VPS_USER")
VPS_HOST = os.environ.get("VPS_HOST")
SSH_OPTS = "-o StrictHostKeyChecking=no -o ConnectTimeout=30"
KEEP_ON_SERVER = 2  # keep latest + 1 previous

# Lists copied from original scripts; you may modify these lists in the repo, or set a config
LOCAL_PACKAGES = [
    "gghelper",
    "gtk2",
    "awesome-freedesktop-git",
    "lain-git",
    "awesome-rofi",
    "nordic-backgrounds",
    "awesome-copycats-manjaro",
    "i3lock-fancy-git",
    "ttf-font-awesome-5",
    "nvidia-driver-assistant",
    "grayjay-bin",
]

AUR_PACKAGES = [
    "libinput-gestures",
    "qt5-styleplugins",
    "urxvt-resize-font-git",
    "i3lock-color",
    "raw-thumbnailer",
    "gsconnect",
    "awesome-git",
    "tilix-git",
    "tamzen-font",
    "betterlockscreen",
    "nordic-theme",
    "nordic-darker-theme",
    "geany-nord-theme",
    "nordzy-icon-theme",
    "oh-my-posh-bin",
    "fish-done",
    "find-the-command",
    "p7zip-gui",
    "qownnotes",
    "xorg-font-utils",
    "xnviewmp",
    "simplescreenrecorder",
    "gtkhash-thunar",
    "a4tech-bloody-driver-git",
    "nordic-bluish-accent-theme",
    "nordic-bluish-accent-standard-buttons-theme",
    "nordic-polar-standard-buttons-theme",
    "nordic-standard-buttons-theme",
    "nordic-darker-standard-buttons-theme",
]

# allow overriding lists by environment variables if needed (optional)
# -------------------------
# Helpers
# -------------------------
def run(cmd: List[str], check=True, capture=False, env=None, cwd=None, timeout=None) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    try:
        if capture:
            return subprocess.run(cmd, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd, timeout=timeout, text=True)
        else:
            return subprocess.run(cmd, check=check, env=env, cwd=cwd, timeout=timeout)
    except subprocess.CalledProcessError as e:
        if capture:
            print(f"[ERR] Command failed: {' '.join(cmd)}\nstdout:{e.stdout}\nstderr:{e.stderr}", file=sys.stderr)
        else:
            print(f"[ERR] Command failed: {' '.join(cmd)} (returncode {e.returncode})", file=sys.stderr)
        raise
    except subprocess.TimeoutExpired as e:
        print(f"[ERR] Command timed out: {' '.join(cmd)}", file=sys.stderr)
        raise

def log_info(msg: str):
    print(f"[INFO] {msg}")

def log_ok(msg: str):
    print(f"[OK] {msg}")

def log_warn(msg: str):
    print(f"[WARN] {msg}")

def log_err(msg: str):
    print(f"[ERROR] {msg}", file=sys.stderr)

# -------------------------
# 1. Network diagnostics
# -------------------------
def network_diagnostics():
    log_info("=== Network diagnostics ===")
    # try curl/wget versions
    for tool in ("curl", "wget"):
        try:
            out = run([tool, "--version"], check=True, capture=True)
            first_line = out.stdout.splitlines()[0] if out.stdout else ""
            log_info(f"{tool}: {first_line}")
        except Exception:
            log_warn(f"{tool} not available or failed")
    # test endpoints
    tests = [
        ("GitHub", "https://github.com"),
        ("AUR", "https://aur.archlinux.org"),
        ("ipinfo", "https://ipinfo.io/ip"),
    ]
    for name, url in tests:
        try:
            run(["timeout", "10", "curl", "-I", url], check=True)
            log_info(f"{name} reachable")
        except Exception:
            log_warn(f"{name} not reachable (timeout or error)")
    log_info("=== End diagnostics ===\n")

# -------------------------
# 2. pacman.conf injection
# -------------------------
def inject_custom_repo(name="manjaro-awesome", server=None, siglevel="Never"):
    """
    Insert a custom repo block before Include lines in /etc/pacman.conf.
    Only acts if environment variable ADD_CUSTOM_REPO == "true".
    """
    add_flag = os.environ.get("ADD_CUSTOM_REPO", "true")
    if add_flag.lower() not in ("1", "true", "yes"):
        log_info("Skipping custom repo injection (ADD_CUSTOM_REPO not set).")
        return
    if server is None:
        server = os.environ.get("CUSTOM_REPO_SERVER", "http://example-repo.local/$arch")
    # read file
    conf = Path("/etc/pacman.conf")
    if not conf.exists():
        log_warn("/etc/pacman.conf not found; skipping injection.")
        return
    try:
        content = conf.read_text()
        block = f"\n[{name}]\nSigLevel = {siglevel}\nServer = {server}\n"
        # Insert before first 'Include' or append to end
        if "Include" in content:
            content = re.sub(r"(?m)^Include.*$", block + r"\g<0>", content, count=1)
        else:
            content = content + "\n" + block
        # write using sudo (attempt)
        tmp = tempfile.NamedTemporaryFile(delete=False, mode="w")
        tmp.write(content)
        tmp.close()
        try:
            run(["sudo", "mv", tmp.name, str(conf)])
            run(["sudo", "chmod", "644", str(conf)])
            log_ok(f"Inserted custom repo [{name}] into /etc/pacman.conf")
        except Exception:
            # fallback: try direct write (if running as root)
            try:
                os.replace(tmp.name, str(conf))
                log_ok(f"Inserted custom repo [{name}] directly into /etc/pacman.conf")
            except Exception as e:
                log_err(f"Failed to write /etc/pacman.conf: {e}")
                raise
    except Exception as e:
        log_err(f"inject_custom_repo error: {e}")


# -------------------------
# 3. Ensure yay is installed
# -------------------------
def ensure_yay():
    try:
        run(["yay", "--version"], capture=True)
        log_info("yay detected.")
        return
    except Exception:
        log_info("Installing yay (automated). This may take a while.")
    tmp = Path("/tmp/yay_install")
    try:
        if tmp.exists():
            shutil.rmtree(tmp)
        run(["git", "clone", "https://aur.archlinux.org/yay.git", str(tmp)], check=True)
        # build as current user: run makepkg -si --noconfirm
        cwd = str(tmp)
        run(["makepkg", "-si", "--noconfirm"], cwd=cwd)
        log_ok("yay installed.")
    except Exception as e:
        log_warn(f"yay installation failed: {e}. Trying 'go install' fallback.")
        try:
            run(["pacman", "-S", "--noconfirm", "go"])
            run(["/bin/sh", "-c", "go install github.com/Jguer/yay@latest"])
            log_ok("yay installed by go install (best-effort).")
        except Exception as ex:
            log_err(f"Fallback install failed too: {ex}")
    finally:
        try:
            if tmp.exists():
                shutil.rmtree(tmp)
        except Exception:
            pass

# -------------------------
# 4. Remote server listing & DB fetch
# -------------------------
def get_remote_file_list(vps_user: str, vps_host: str, remote_dir: str) -> List[str]:
    out_file = REPO_ROOT / "remote_files.txt"
    out_file.write_text("")  # reset
    cmd = f"ssh {SSH_OPTS} {vps_user}@{vps_host} 'find {shlex.quote(remote_dir)} -maxdepth 1 -type f -printf \"%f\n\" 2>/dev/null | sort'"
    try:
        cp = run(cmd, check=True, capture=True, shell=False, env=None)
    except TypeError:
        # fallback: use list
        cp = run(shlex.split(cmd), check=True, capture=True)
    txt = cp.stdout if isinstance(cp, subprocess.CompletedProcess) else ""
    out_file.write_text(txt)
    log_info(f"Downloaded remote file list to {out_file} ({len(txt.splitlines())} lines).")
    return [line.strip() for line in txt.splitlines() if line.strip()]

def fetch_remote_db(vps_user: str, vps_host: str, remote_dir: str, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    remote_db = f"{remote_dir}/{REPO_DB_NAME}.db.tar.gz"
    dest = output_dir / f"{REPO_DB_NAME}.db.tar.gz"
    try:
        run(shlex.split(f"scp {SSH_OPTS} {vps_user}@{vps_host}:{remote_db} {str(dest)}"), check=True)
        log_ok(f"Downloaded remote DB to {dest}")
    except Exception:
        log_warn("Could not download remote DB (maybe first run). Continuing.")

# -------------------------
# 5. Utility: parse version from .SRCINFO or PKGBUILD
# -------------------------
def parse_version_from_srcinfo(srcinfo_text: str) -> Tuple[str, str]:
    """
    Parse the first pkgver and pkgrel found in a .SRCINFO string.
    Return (pkgver, pkgrel) or ('unknown', '1') if not found.
    """
    pkgver = "unknown"
    pkgrel = "1"
    for line in srcinfo_text.splitlines():
        m = re.match(r'\s*pkgver\s*=\s*(\S+)', line)
        if m:
            pkgver = m.group(1).strip()
            break
    for line in srcinfo_text.splitlines():
        m = re.match(r'\s*pkgrel\s*=\s*(\S+)', line)
        if m:
            pkgrel = m.group(1).strip()
            break
    return pkgver, pkgrel

def get_version_for_dir(pkgdir: Path) -> Tuple[str, str]:
    """
    Try to get pkgver/pkgrel from .SRCINFO or by running `makepkg --printsrcinfo`.
    """
    try:
        if (pkgdir / ".SRCINFO").exists():
            text = (pkgdir / ".SRCINFO").read_text()
            return parse_version_from_srcinfo(text)
        # try makepkg --printsrcinfo
        cp = run(["makepkg", "--printsrcinfo"], check=True, capture=True, cwd=str(pkgdir))
        text = cp.stdout
        if text:
            return parse_version_from_srcinfo(text)
        # fallback to reading PKGBUILD lines
        if (pkgdir / "PKGBUILD").exists():
            text = (pkgdir / "PKGBUILD").read_text()
            m1 = re.search(r'^\s*pkgver\s*=\s*(["\']?)(.+?)\1\s*$', text, re.M)
            m2 = re.search(r'^\s*pkgrel\s*=\s*(["\']?)(.+?)\1\s*$', text, re.M)
            pkgver = m1.group(2) if m1 else "unknown"
            pkgrel = m2.group(2) if m2 else "1"
            return pkgver, pkgrel
    except Exception as e:
        log_warn(f"get_version_for_dir: failed for {pkgdir}: {e}")
    return "unknown", "1"

# -------------------------
# 6. Check presence on server helpers
# -------------------------
def is_version_on_server(pkgname: str, version: str, remote_list: List[str]) -> bool:
    # match lines like "pkgname-1.2.3-1-any.pkg.tar.zst"
    prefix = f"{pkgname}-{version}-"
    for f in remote_list:
        if f.startswith(prefix):
            return True
    return False

def is_any_version_on_server(pkgname: str, remote_list: List[str]) -> bool:
    prefix = f"{pkgname}-"
    for f in remote_list:
        if f.startswith(prefix):
            return True
    return False

# -------------------------
# 7. Install build deps (best-effort)
# -------------------------
def install_deps_from_srcinfo(pkgdir: Path, is_aur: bool):
    """Extract depends/makedepends from .SRCINFO or PKGBUILD and attempt to install them."""
    log_info(f"Analyzing dependencies for {pkgdir.name}")
    depends = []
    makedepends = []
    try:
        # prefer .SRCINFO if present
        if (pkgdir / ".SRCINFO").exists():
            text = (pkgdir / ".SRCINFO").read_text()
            # simple extraction: lines with "depends = ..." "makedepends = ..."
            for line in text.splitlines():
                m_dep = re.match(r'^\s*depends\s*=\s*(.+)$', line)
                if m_dep:
                    depends.append(m_dep.group(1).strip())
                m_make = re.match(r'^\s*makedepends\s*=\s*(.+)$', line)
                if m_make:
                    makedepends.append(m_make.group(1).strip())
        else:
            # fallback parse PKGBUILD by sourcing via makepkg --printsrcinfo
            cp = run(["makepkg", "--printsrcinfo"], check=True, capture=True, cwd=str(pkgdir))
            text = cp.stdout
            for line in text.splitlines():
                m_dep = re.match(r'^\s*depends\s*=\s*(.+)$', line)
                if m_dep:
                    depends.append(m_dep.group(1).strip())
                m_make = re.match(r'^\s*makedepends\s*=\s*(.+)$', line)
                if m_make:
                    makedepends.append(m_make.group(1).strip())
    except Exception:
        log_warn("Could not extract dependencies via .SRCINFO or makepkg; trying heuristic parse of PKGBUILD")
        try:
            text = (pkgdir / "PKGBUILD").read_text()
            m_deps_block = re.search(r'depends\s*=\s*\(([^)]*)\)', text, re.S)
            if m_deps_block:
                deps_block = m_deps_block.group(1)
                for item in re.findall(r'["\']?([^\s"\'()]+)["\']?', deps_block):
                    depends.append(item)
            m_makedep_block = re.search(r'makedepends\s*=\s*\(([^)]*)\)', text, re.S)
            if m_makedep_block:
                block = m_makedep_block.group(1)
                for item in re.findall(r'["\']?([^\s"\'()]+)["\']?', block):
                    makedepends.append(item)
        except Exception:
            pass

    # combine and deduplicate
    all_deps = list(dict.fromkeys(depends + makedepends))
    if not all_deps:
        log_info("No explicit dependencies found.")
        return

    # Clean crud like relational operators and providers
    cleaned = []
    for d in all_deps:
        dclean = re.sub(r'[<>=].*', '', d).strip()
        if not dclean:
            continue
        # provider conversions (example)
        if dclean == "jack":
            dclean = "jack2"
        cleaned.append(dclean)
    cleaned = list(dict.fromkeys(cleaned))

    # figure out which are official vs AUR
    official = []
    aur = []
    for d in cleaned:
        try:
            # pacman -Si returns 0 if package exists in official repos
            run(["pacman", "-Si", d], check=True, capture=True)
            official.append(d)
        except Exception:
            aur.append(d)

    # install official deps
    if official:
        log_info("Installing official deps: " + " ".join(official))
        try:
            run(["sudo", "pacman", "-S", "--needed", "--noconfirm"] + official)
        except Exception as e:
            log_warn(f"Official deps install had issues: {e}")

    # install aur deps via yay
    if aur:
        log_info("Installing AUR deps via yay (best effort): " + " ".join(aur))
        for d in aur:
            try:
                run(["yay", "-S", "--asdeps", "--needed", "--noconfirm", d])
            except Exception as e:
                log_warn(f"yay install of {d} failed: {e}")

# -------------------------
# 8. Build package (AUR or local)
# -------------------------
def build_package(pkg: str, is_aur: bool, remote_list: List[str], ssh_repo_url: str):
    log_info(f"Processing package: {pkg} (AUR: {is_aur})")
    REPO_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pkg_dir: Optional[Path] = None
    if is_aur:
        # clone AUR into build_aur
        build_aur = REPO_ROOT / "build_aur"
        if build_aur.exists():
            shutil.rmtree(build_aur, ignore_errors=True)
        build_aur.mkdir(parents=True, exist_ok=True)
        dest = build_aur / pkg
        try:
            run(["git", "clone", f"https://aur.archlinux.org/{pkg}.git", str(dest)], check=True)
            pkg_dir = dest
        except Exception:
            log_err(f"AUR clone failed for {pkg}; skipping.")
            return
    else:
        local = REPO_ROOT / pkg
        if not local.exists() or not local.is_dir():
            log_warn(f"Local package directory missing: {pkg}; skipping.")
            return
        pkg_dir = local

    # attempt early skip by checking PKGBUILD/.SRCINFO version
    try:
        v_pkgver, v_pkgrel = get_version_for_dir(pkg_dir)
        current_version = f"{v_pkgver}-{v_pkgrel}"
        if v_pkgver != "unknown" and is_version_on_server(pkg, current_version, remote_list):
            log_warn(f"{pkg} {current_version} already on server -> skipping (early).")
            # cleanup if AUR
            if is_aur and (REPO_ROOT / "build_aur" / pkg).exists():
                shutil.rmtree(REPO_ROOT / "build_aur" / pkg, ignore_errors=True)
            return
    except Exception as e:
        log_warn(f"Early version check failed for {pkg}: {e}")

    # install deps
    try:
        install_deps_from_srcinfo(pkg_dir, is_aur)
    except Exception as e:
        log_warn(f"Dependency installation error for {pkg}: {e}")

    # run makepkg -od to download sources and populate .SRCINFO
    try:
        run(["makepkg", "-od", "--noconfirm"], cwd=str(pkg_dir))
    except Exception as e:
        log_err(f"Source download or version detection failed for {pkg}: {e}")
        if is_aur:
            # cleanup
            try:
                shutil.rmtree(REPO_ROOT / "build_aur" / pkg, ignore_errors=True)
            except Exception:
                pass
        return

    # re-evaluate version via .SRCINFO or makepkg --printsrcinfo
    try:
        cp = run(["makepkg", "--printsrcinfo"], check=True, capture=True, cwd=str(pkg_dir))
        text = cp.stdout
        v_pkgver, v_pkgrel = parse_version_from_srcinfo(text)
    except Exception:
        v_pkgver, v_pkgrel = get_version_for_dir(pkg_dir)
    current_version = f"{v_pkgver}-{v_pkgrel}"

    # final skip - server check
    if v_pkgver != "unknown" and is_version_on_server(pkg, current_version, remote_list):
        log_warn(f"{pkg} {current_version} already on server -> skipping (post-check).")
        if is_aur:
            shutil.rmtree(REPO_ROOT / "build_aur" / pkg, ignore_errors=True)
        return

    # Build flags
    makepkg_flags = ["-si", "--noconfirm", "--clean"]
    # for big packages we might want --nocheck; keep conservative default
    if any(token in pkg for token in ("gtk", "qt", "chromium")):
        makepkg_flags += ["--nocheck"]

    # run makepkg with timeout (best-effort)
    timeout_seconds = 3600
    if "gtk" in pkg or "qt" in pkg or "chromium" in pkg:
        timeout_seconds = 7200
    elif "simplescreenrecorder" in pkg:
        timeout_seconds = 5400

    try:
        log_info(f"Building {pkg} (version {current_version})")
        run(["timeout", str(timeout_seconds), "makepkg"] + makepkg_flags, cwd=str(pkg_dir))
    except Exception as e:
        log_err(f"Build failed for {pkg}: {e}")
        # cleanup AUR clone
        if is_aur:
            shutil.rmtree(REPO_ROOT / "build_aur" / pkg, ignore_errors=True)
        return

    # move built artifacts to OUTPUT_DIR
    built_any = False
    try:
        for f in pkg_dir.glob("*.pkg.tar.*"):
            target = OUTPUT_DIR / f.name
            shutil.move(str(f), str(target))
            log_ok(f"Built artifact moved to {target}")
            built_any = True
    except Exception as e:
        log_warn(f"Could not move build artifacts for {pkg}: {e}")

    # record package for cleaning
    if built_any:
        packages_to_clean_path = REPO_ROOT / "packages_to_clean.txt"
        with packages_to_clean_path.open("a") as fh:
            fh.write(pkg + "\n")

    # If it's a local package and build succeeded, update PKGBUILD/pkgrel/pkgver and push to repo via clone method
    if built_any and not is_aur:
        try:
            # update pkgver/pkgrel in local PKGBUILD (reflecting what makepkg determined)
            pkgb = pkg_dir / "PKGBUILD"
            if pkgb.exists():
                text = pkgb.read_text()
                # attempt to replace simple assignments; be cautious
                text = re.sub(r'(?m)^\s*pkgver\s*=.*$', f"pkgver={v_pkgver}", text)
                text = re.sub(r'(?m)^\s*pkgrel\s*=.*$', f"pkgrel={v_pkgrel}", text)
                pkgb.write_text(text)
                # regenerate .SRCINFO
                run(["makepkg", "--printsrcinfo"], check=True, capture=False, cwd=str(pkg_dir))
            # clone publish repo to temp, copy files, commit and push
            tmpdir = Path(tempfile.mkdtemp(prefix="git_publish_"))
            try:
                run(["git", "clone", ssh_repo_url, str(tmpdir)], check=True)
                target_pkg_dir = tmpdir / pkg
                target_pkg_dir.mkdir(parents=True, exist_ok=True)
                # copy PKGBUILD and .SRCINFO
                if (pkg_dir / "PKGBUILD").exists():
                    shutil.copy2(str(pkg_dir / "PKGBUILD"), str(target_pkg_dir / "PKGBUILD"))
                if (pkg_dir / ".SRCINFO").exists():
                    shutil.copy2(str(pkg_dir / ".SRCINFO"), str(target_pkg_dir / ".SRCINFO"))
                # git commit if changes
                # configure git user
                run(["git", "config", "--global", "user.name", "GitHub Action Bot"])
                run(["git", "config", "--global", "user.email", "action@github.com"])
                # check diff
                # do this inside repo
                cp = run(["git", "status", "--porcelain"], check=True, capture=True, cwd=str(tmpdir))
                if cp.stdout.strip() == "":
                    log_info("No changes to commit in publish repo.")
                else:
                    run(["git", "add", f"{pkg}/PKGBUILD", f"{pkg}/.SRCINFO"], cwd=str(tmpdir))
                    run(["git", "commit", "-m", f"Auto-update: {pkg} updated to {current_version} [skip ci]"], cwd=str(tmpdir))
                    try:
                        run(["git", "push"], cwd=str(tmpdir))
                        log_ok("Pushed updated PKGBUILD/.SRCINFO to remote publish repo.")
                    except Exception as e:
                        log_err(f"Git push failed: {e}")
            finally:
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
        except Exception as e:
            log_warn(f"Auto-update push for {pkg} failed: {e}")

    # cleanup AUR clone
    if is_aur:
        try:
            shutil.rmtree(REPO_ROOT / "build_aur" / pkg, ignore_errors=True)
        except Exception:
            pass

# -------------------------
# 9. Upload artifacts and retention on remote
# -------------------------
def upload_and_update_db(vps_user: str, vps_host: str, remote_dir: str):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pkgfiles = list(OUTPUT_DIR.glob("*.pkg.tar.*"))
    if not pkgfiles:
        log_info("No new artifacts to upload.")
        return

    # create/expand repo DB locally
    try:
        os.chdir(str(OUTPUT_DIR))
        if (OUTPUT_DIR / f"{REPO_DB_NAME}.db.tar.gz").exists():
            run(["repo-add", f"{REPO_DB_NAME}.db.tar.gz"] + [str(p.name) for p in pkgfiles])
            log_info("Added new packages to existing DB.")
        else:
            run(["repo-add", f"{REPO_DB_NAME}.db.tar.gz"] + [str(p.name) for p in pkgfiles])
            log_info("Created new DB with built packages.")
    except Exception as e:
        log_warn(f"repo-add failed: {e}")

    # scp files
    try:
        files_to_send = [str(p) for p in OUTPUT_DIR.iterdir() if p.is_file()]
        scp_cmd = ["scp"] + shlex.split(SSH_OPTS) + files_to_send + [f"{vps_user}@{vps_host}:{remote_dir}/"]
        log_info("Uploading artifacts to remote...")
        run(scp_cmd)
        log_ok("Upload successful.")
    except Exception as e:
        log_warn(f"Upload failed once: {e}. Retrying...")
        time.sleep(3)
        try:
            run(scp_cmd)
            log_ok("Upload successful on retry.")
        except Exception as e2:
            log_err(f"Upload failed again: {e2}")
            raise

    # server retention: for each package in packages_to_clean.txt, keep latest 2, delete older
    packages_to_clean_path = REPO_ROOT / "packages_to_clean.txt"
    if not packages_to_clean_path.exists():
        log_info("No packages_to_clean.txt found; skipping server pruning.")
        return
    with packages_to_clean_path.open() as fh:
        pkgs = [ln.strip() for ln in fh if ln.strip()]
    if not pkgs:
        log_info("packages_to_clean.txt empty; skipping pruning.")
        return
    for pkg in pkgs:
        # remote command: list files sorted by time or by version name; we'll use ls -t and delete from +3
        remote_cmd = f"cd {shlex.quote(remote_dir)} && ls -1t {shlex.quote(pkg)}-*.pkg.tar.* 2>/dev/null | tail -n +{KEEP_ON_SERVER+1} | xargs -r rm -f || true"
        full_cmd = f"ssh {SSH_OPTS} {vps_user}@{vps_host} {shlex.quote(remote_cmd)}"
        try:
            run(shlex.split(full_cmd))
            log_info(f"Pruned remote older packages for {pkg} (kept {KEEP_ON_SERVER}).")
        except Exception as e:
            log_warn(f"Remote pruning command for {pkg} had issues: {e}")

# -------------------------
# 10. Main
# -------------------------
def main():
    if not VPS_USER or not VPS_HOST:
        log_err("VPS_USER and VPS_HOST environment variables must be set.")
        sys.exit(2)

    network_diagnostics()

    # inject custom repo (makes pacman faster with prebuilt binaries)
    inject_custom_repo()

    # Ensure yay present
    ensure_yay()

    # get remote list and fetch DB
    try:
        remote_list = get_remote_file_list(VPS_USER, VPS_HOST, REMOTE_DIR)
    except Exception:
        remote_list = []
    fetch_remote_db(VPS_USER, VPS_HOST, REMOTE_DIR, OUTPUT_DIR)

    # reset packages_to_clean
    ptc = REPO_ROOT / "packages_to_clean.txt"
    if ptc.exists():
        ptc.unlink()
    ptc.write_text("")

    # Build AUR packages first (like your scripts)
    log_info("--- AUR PACKAGES ---")
    for pkg in AUR_PACKAGES:
        try:
            build_package(pkg, True, remote_list, SSH_REPO_URL)
        except Exception as e:
            log_warn(f"Failure processing AUR package {pkg}: {e}")

    # then local packages
    log_info("--- LOCAL PACKAGES ---")
    for pkg in LOCAL_PACKAGES:
        try:
            build_package(pkg, False, remote_list, SSH_REPO_URL)
        except Exception as e:
            log_warn(f"Failure processing local package {pkg}: {e}")

    # final upload & DB update and pruning
    try:
        upload_and_update_db(VPS_USER, VPS_HOST, REMOTE_DIR)
    except Exception as e:
        log_err(f"upload_and_update_db failed: {e}")

    log_ok("Builder finished.")

if __name__ == "__main__":
    main()
