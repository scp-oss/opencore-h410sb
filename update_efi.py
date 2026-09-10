#!/usr/bin/env python3
"""
Update kexts and OpenCorePkg binaries in this EFI checkout from upstream
GitHub releases. Pure stdlib, no dependencies - run with the system python3.

Usage (run from your Mac, with internet access, against a local clone of
this repo - NOT from a sandbox with no GitHub access):

    python3 update_efi.py --list                 # just show local vs latest
    python3 update_efi.py --dry-run               # same, more detail
    python3 update_efi.py                         # interactive, asks before each change
    python3 update_efi.py --yes                   # apply everything without asking
    python3 update_efi.py --only Lilu,WhateverGreen
    python3 update_efi.py --root /path/to/opencore-h410sb

What this deliberately does NOT touch, and why:
  - config.plist          - your own customization, never auto-edited
  - ACPI/*.aml             - compiled per-board SSDTs, not a generic download
  - Resources/             - OpenCanopy theme, may be customized
  - Kexts/UTBDefault.kext  - USB port map generated for YOUR board via the
                             USBToolBox app / UTBMap, not a generic release
  - Kexts/XHCI-unsupported.kext - upstream source unconfirmed, add it to
                             COMPONENTS yourself once you've verified where
                             it actually comes from; guessing a repo here
                             would silently fetch the wrong thing

After running, review with `git diff --stat` / `git status` before
committing - this script never commits or pushes on its own.
"""
import argparse
import json
import os
import plistlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile

API_ROOT = "https://api.github.com/repos"
UA = "Mozilla/5.0 (compatible; opencore-h410sb-updater/1.0)"

# Each entry updates one or more kext bundles that all ship in the same
# upstream release zip. "repo" is unauthenticated GitHub API - rate limited
# to 60 req/hour, plenty for a one-shot update run.
COMPONENTS = [
    {"name": "Lilu", "repo": "acidanthera/Lilu", "kexts": ["Lilu.kext"]},
    {"name": "WhateverGreen", "repo": "acidanthera/WhateverGreen", "kexts": ["WhateverGreen.kext"]},
    {"name": "VirtualSMC", "repo": "acidanthera/VirtualSMC",
     "kexts": ["VirtualSMC.kext", "SMCProcessor.kext", "SMCSuperIO.kext"]},
    {"name": "NVMeFix", "repo": "acidanthera/NVMeFix", "kexts": ["NVMeFix.kext"]},
    {"name": "RestrictEvents", "repo": "acidanthera/RestrictEvents", "kexts": ["RestrictEvents.kext"]},
    {"name": "BrcmPatchRAM", "repo": "acidanthera/BrcmPatchRAM", "kexts": ["BlueToolFixup.kext"]},
    {"name": "USBToolBox", "repo": "USBToolBox/kext", "kexts": ["USBToolBox.kext"]},
    {"name": "IntelMausiEthernet", "repo": "Mieze/IntelMausiEthernet", "kexts": ["IntelMausiEthernet.kext"]},
]
OPENCORE_REPO = "acidanthera/OpenCorePkg"


def gh_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def latest_release(repo):
    data = gh_get(f"{API_ROOT}/{repo}/releases/latest")
    tag = data["tag_name"].lstrip("v")
    assets = [(a["name"], a["browser_download_url"]) for a in data.get("assets", [])]
    return tag, assets


def pick_asset(assets):
    def is_release_zip(name):
        low = name.lower()
        return low.endswith(".zip") and "debug" in low is False
    release_named = [a for a in assets if "release" in a[0].lower() and a[0].lower().endswith(".zip")]
    if release_named:
        return release_named[0]
    plain_zips = [a for a in assets if a[0].lower().endswith(".zip") and "debug" not in a[0].lower()
                  and "source code" not in a[0].lower()]
    if plain_zips:
        return plain_zips[0]
    raise RuntimeError(f"no usable .zip asset found among: {[a[0] for a in assets]}")


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def find_in_tree(root, name):
    for dirpath, dirnames, _ in os.walk(root):
        if name in dirnames:
            return os.path.join(dirpath, name)
    return None


def local_kext_version(kext_path):
    info = os.path.join(kext_path, "Contents", "Info.plist")
    if not os.path.isfile(info):
        return None
    try:
        with open(info, "rb") as f:
            d = plistlib.load(f)
        return d.get("CFBundleShortVersionString") or d.get("CFBundleVersion")
    except Exception:
        return None


def local_opencore_version(efi_path):
    # Tried extracting this from embedded strings in the binary (ASCII and
    # UTF-16LE, since OpenCore's UEFI text console strings are UTF-16) -
    # neither reliably contains a bare X.Y.Z version string; what's there
    # is either build-tool version noise or a template with the version
    # substituted in only at render time. Not worth faking - the real
    # current version is visible on the OpenCore boot picker screen itself.
    return None


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def update_kext_component(component, root, dry_run, assume_yes, list_only):
    name = component["name"]
    kexts_dir = os.path.join(root, "Kexts")
    try:
        latest_tag, assets = latest_release(component["repo"])
    except Exception as e:
        print(f"[{name}] could not check latest release ({component['repo']}): {e}")
        return

    local_versions = {}
    for kext in component["kexts"]:
        local_versions[kext] = local_kext_version(os.path.join(kexts_dir, kext))

    outdated = any(v != latest_tag for v in local_versions.values())
    status = "outdated" if outdated else "up to date"
    print(f"[{name}] local={local_versions} latest={latest_tag} -> {status}")

    if list_only or not outdated:
        return
    if dry_run:
        print(f"[{name}] would update -> {latest_tag} (dry-run, nothing changed)")
        return
    if not confirm(f"[{name}] apply update to {latest_tag}?", assume_yes):
        print(f"[{name}] skipped")
        return

    asset_name, asset_url = pick_asset(assets)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, asset_name)
        print(f"[{name}] downloading {asset_name} ...")
        download(asset_url, zip_path)
        extract_dir = os.path.join(tmp, "extracted")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)

        for kext in component["kexts"]:
            src = find_in_tree(extract_dir, kext)
            if not src:
                print(f"[{name}] WARNING: {kext} not found inside {asset_name}, left untouched")
                continue
            dst = os.path.join(kexts_dir, kext)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"[{name}] updated {kext} -> {latest_tag}")


def update_opencore(root, dry_run, assume_yes, list_only):
    try:
        latest_tag, assets = latest_release(OPENCORE_REPO)
    except Exception as e:
        print(f"[OpenCorePkg] could not check latest release: {e}")
        return

    oc_efi = os.path.join(root, "OpenCore.efi")
    local_ver = local_opencore_version(oc_efi)
    print(f"[OpenCorePkg] local={local_ver or 'unknown'} latest={latest_tag}")

    if list_only:
        return
    if dry_run:
        print("[OpenCorePkg] dry-run, nothing changed")
        return
    # Always ask explicitly for OpenCorePkg itself, even if the version
    # looks the same - version string detection is best-effort, and a
    # major OpenCore bump can require config.plist schema changes that
    # this script does not make.
    if not confirm(f"[OpenCorePkg] fetch {latest_tag} and replace OpenCore.efi + matching Drivers/*.efi?",
                    assume_yes):
        print("[OpenCorePkg] skipped")
        return

    asset_name, asset_url = pick_asset(assets)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, asset_name)
        print(f"[OpenCorePkg] downloading {asset_name} ...")
        download(asset_url, zip_path)
        extract_dir = os.path.join(tmp, "extracted")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)

        new_oc_dir = find_in_tree(extract_dir, "OC")
        # OpenCorePkg zip layout: X64/EFI/OC/... - walk to find the one
        # whose sibling contains OpenCore.efi (X64, not IA32).
        candidate = None
        for dirpath, _, filenames in os.walk(extract_dir):
            if "OpenCore.efi" in filenames and os.path.basename(os.path.dirname(dirpath)) == "EFI" \
                    and "X64" in dirpath.split(os.sep):
                candidate = dirpath
                break
        if not candidate:
            print("[OpenCorePkg] could not locate X64/EFI/OC in the release zip, aborting this component")
            return

        shutil.copy2(os.path.join(candidate, "OpenCore.efi"), oc_efi)
        print(f"[OpenCorePkg] updated OpenCore.efi -> {latest_tag}")

        local_drivers_dir = os.path.join(root, "Drivers")
        new_drivers_dir = os.path.join(candidate, "Drivers")
        if os.path.isdir(local_drivers_dir) and os.path.isdir(new_drivers_dir):
            for fname in os.listdir(local_drivers_dir):
                src = os.path.join(new_drivers_dir, fname)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(local_drivers_dir, fname))
                    print(f"[OpenCorePkg] updated Drivers/{fname}")
                else:
                    print(f"[OpenCorePkg] WARNING: Drivers/{fname} has no counterpart in {latest_tag}, left as-is")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="path to the opencore-h410sb checkout (default: cwd)")
    ap.add_argument("--only", help="comma-separated component names to limit to (e.g. Lilu,WhateverGreen)")
    ap.add_argument("--dry-run", action="store_true", help="only report what would change")
    ap.add_argument("--list", action="store_true", help="only print local vs latest versions, no prompts")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation before applying")
    ap.add_argument("--include-opencore", action="store_true",
                     help="also check/update OpenCore.efi + Drivers/*.efi (higher risk, see module docstring)")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(os.path.join(root, "Kexts")):
        sys.exit(f"'{root}' doesn't look like the EFI/OC checkout (no Kexts/ found)")

    only = set(n.strip() for n in args.only.split(",")) if args.only else None

    for component in COMPONENTS:
        if only and component["name"] not in only:
            continue
        update_kext_component(component, root, args.dry_run, args.yes, args.list)

    if args.include_opencore and (not only or "OpenCorePkg" in only):
        update_opencore(root, args.dry_run, args.yes, args.list)


if __name__ == "__main__":
    main()
