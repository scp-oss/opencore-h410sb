"""
Core logic for the EFI updater web tool - no network calls are made at
import time, everything is a plain function so it can be unit tested
without hitting GitHub. Pure stdlib (json/urllib/zipfile/plistlib/hashlib),
deliberately no third-party dependencies.

This runs on the machine that actually has internet access and the real
EFI folder (your Mac) - not in a sandbox with no GitHub access.
"""
import hashlib
import json
import os
import plistlib
import shutil
import tempfile
import urllib.request
import zipfile

API_ROOT = "https://api.github.com/repos"
UA = "Mozilla/5.0 (compatible; opencore-h410sb-updater/1.0)"
STATE_FILENAME = ".efi_updater_state.json"

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

# Per-board artifacts that must never be silently overwritten by an
# upstream "generic" version - they're generated/curated specifically for
# this machine, not a thing with an upstream release to pull.
MANUAL_ONLY_KEXTS = {"UTBDefault.kext", "XHCI-unsupported.kext"}


# ---------------------------------------------------------------- GitHub --

def _gh_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def resolve_release(repo, channel):
    """channel: 'stable' or 'prerelease'. Returns (tag, assets, html_url)."""
    if channel == "stable":
        data = _gh_get_json(f"{API_ROOT}/{repo}/releases/latest")
    else:
        releases = _gh_get_json(f"{API_ROOT}/{repo}/releases?per_page=5")
        if not releases:
            raise RuntimeError(f"{repo} has no releases at all")
        data = releases[0]  # GitHub lists newest first, prereleases included
    tag = data["tag_name"].lstrip("v")
    assets = [(a["name"], a["browser_download_url"]) for a in data.get("assets", [])]
    return tag, assets, data.get("html_url", "")


def pick_asset(assets):
    release_named = [a for a in assets if "release" in a[0].lower() and a[0].lower().endswith(".zip")]
    if release_named:
        return release_named[0]
    plain_zips = [a for a in assets if a[0].lower().endswith(".zip")
                  and "debug" not in a[0].lower() and "source code" not in a[0].lower()]
    if plain_zips:
        return plain_zips[0]
    raise RuntimeError(f"no usable .zip asset among: {[a[0] for a in assets]}")


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def repo_default_branch(repo):
    return _gh_get_json(f"{API_ROOT}/{repo}").get("default_branch", "master")


def download_repo_archive(repo, dest_zip, ref=None):
    """Whole-repo snapshot (for theme repos, which aren't released as
    versioned zips the way kexts are - just 'give me Resources/ as it is
    on the default branch')."""
    ref = ref or repo_default_branch(repo)
    url = f"https://api.github.com/repos/{repo}/zipball/{ref}"
    download(url, dest_zip)


# --------------------------------------------------------- filesystem ----

def find_in_tree(root, name):
    for dirpath, dirnames, _ in os.walk(root):
        if name in dirnames:
            return os.path.join(dirpath, name)
    return None


def find_dir_named(root, name):
    """Like find_in_tree but also matches a directory whose name merely
    ends with the target (zipball archives prefix the repo dir with a
    commit hash, e.g. 'acidanthera-OcBinaryData-abcdef')."""
    exact = find_in_tree(root, name)
    if exact:
        return exact
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            if d == name:
                return os.path.join(dirpath, d)
    return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def load_state(root):
    path = os.path.join(root, STATE_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(root, state):
    path = os.path.join(root, STATE_FILENAME)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ------------------------------------------------------------------ scan --

def scan_root(root):
    """Read-only: no network, no writes. Returns a JSON-able dict describing
    what's on disk right now."""
    if not os.path.isdir(os.path.join(root, "Kexts")):
        raise RuntimeError(f"'{root}' doesn't look like an EFI/OC checkout (no Kexts/ found)")

    state = load_state(root)
    kexts_dir = os.path.join(root, "Kexts")

    components = []
    known_kexts = set()
    for component in COMPONENTS:
        entry = {"name": component["name"], "repo": component["repo"], "kexts": []}
        for kext in component["kexts"]:
            known_kexts.add(kext)
            path = os.path.join(kexts_dir, kext)
            entry["kexts"].append({
                "bundle": kext,
                "present": os.path.isdir(path),
                "local_version": local_kext_version(path) if os.path.isdir(path) else None,
            })
        components.append(entry)

    manual_kexts = []
    other_kexts = []
    if os.path.isdir(kexts_dir):
        for name in sorted(os.listdir(kexts_dir)):
            if not name.endswith(".kext"):
                continue
            if name in MANUAL_ONLY_KEXTS:
                manual_kexts.append(name)
            elif name not in known_kexts:
                other_kexts.append(name)

    oc_efi_path = os.path.join(root, "OpenCore.efi")
    oc_info = {"present": os.path.isfile(oc_efi_path)}
    if oc_info["present"]:
        current_hash = sha256_of(oc_efi_path)
        oc_info["sha256"] = current_hash
        recorded = state.get("opencore", {})
        oc_info["last_known_version"] = recorded.get("version")
        oc_info["last_known_channel"] = recorded.get("channel")
        oc_info["changed_since_last_update"] = recorded.get("sha256") != current_hash

    drivers_dir = os.path.join(root, "Drivers")
    drivers = []
    if os.path.isdir(drivers_dir):
        recorded_drivers = state.get("drivers", {})
        for fname in sorted(os.listdir(drivers_dir)):
            fpath = os.path.join(drivers_dir, fname)
            if not os.path.isfile(fpath):
                continue
            current_hash = sha256_of(fpath)
            rec = recorded_drivers.get(fname, {})
            drivers.append({
                "file": fname,
                "sha256": current_hash,
                "last_known_version": rec.get("version"),
                "changed_since_last_update": rec.get("sha256") != current_hash,
            })

    resources_state = state.get("resources_theme", {})

    return {
        "root": root,
        "components": components,
        "manual_only_kexts_present": manual_kexts,
        "other_kexts_present": other_kexts,
        "opencore": oc_info,
        "drivers": drivers,
        "resources_theme": resources_state,
    }


def check_updates(root, channel):
    """Network calls: hits GitHub once per component + once for OpenCorePkg.
    Returns scan_root()'s data plus a 'latest' field per component/opencore."""
    data = scan_root(root)
    for entry in data["components"]:
        try:
            tag, assets, url = resolve_release(entry["repo"], channel)
            entry["latest_version"] = tag
            entry["release_url"] = url
            entry["outdated"] = any(k["local_version"] != tag for k in entry["kexts"])
        except Exception as e:
            entry["error"] = str(e)
    try:
        tag, assets, url = resolve_release(OPENCORE_REPO, channel)
        data["opencore"]["latest_version"] = tag
        data["opencore"]["release_url"] = url
    except Exception as e:
        data["opencore"]["error"] = str(e)
    return data


# --------------------------------------------------------------- apply ---

def apply_kext_component(component_name, root, channel, log):
    component = next((c for c in COMPONENTS if c["name"] == component_name), None)
    if not component:
        raise RuntimeError(f"unknown component '{component_name}'")

    tag, assets, _ = resolve_release(component["repo"], channel)
    asset_name, asset_url = pick_asset(assets)
    log.append(f"[{component_name}] using {asset_name} ({channel}) -> {tag}")

    kexts_dir = os.path.join(root, "Kexts")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, asset_name)
        download(asset_url, zip_path)
        extract_dir = os.path.join(tmp, "extracted")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)

        applied = []
        for kext in component["kexts"]:
            src = find_in_tree(extract_dir, kext)
            if not src:
                log.append(f"[{component_name}] WARNING: {kext} not found in {asset_name}, left untouched")
                continue
            dst = os.path.join(kexts_dir, kext)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            log.append(f"[{component_name}] {kext} -> {tag}")
            applied.append(kext)
    return {"component": component_name, "version": tag, "channel": channel, "applied_kexts": applied}


def apply_opencore(root, channel, parts, log):
    """parts: subset of {'efi', 'drivers', 'resources'}."""
    tag, assets, _ = resolve_release(OPENCORE_REPO, channel)
    asset_name, asset_url = pick_asset(assets)
    log.append(f"[OpenCorePkg] using {asset_name} ({channel}) -> {tag}")

    state = load_state(root)
    result = {"version": tag, "channel": channel, "applied_parts": []}

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, asset_name)
        download(asset_url, zip_path)
        extract_dir = os.path.join(tmp, "extracted")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)

        oc_dir = None
        for dirpath, _, filenames in os.walk(extract_dir):
            if "OpenCore.efi" in filenames and "X64" in dirpath.split(os.sep):
                oc_dir = dirpath
                break
        if not oc_dir:
            raise RuntimeError(f"could not locate X64/EFI/OC inside {asset_name}")

        if "efi" in parts:
            dst = os.path.join(root, "OpenCore.efi")
            shutil.copy2(os.path.join(oc_dir, "OpenCore.efi"), dst)
            state["opencore"] = {"version": tag, "channel": channel, "sha256": sha256_of(dst)}
            log.append(f"[OpenCorePkg] OpenCore.efi -> {tag}")
            result["applied_parts"].append("efi")

        if "drivers" in parts:
            local_drivers_dir = os.path.join(root, "Drivers")
            new_drivers_dir = os.path.join(oc_dir, "Drivers")
            state.setdefault("drivers", {})
            if os.path.isdir(local_drivers_dir) and os.path.isdir(new_drivers_dir):
                for fname in os.listdir(local_drivers_dir):
                    src = os.path.join(new_drivers_dir, fname)
                    dst = os.path.join(local_drivers_dir, fname)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                        state["drivers"][fname] = {"version": tag, "sha256": sha256_of(dst)}
                        log.append(f"[OpenCorePkg] Drivers/{fname} -> {tag}")
                    else:
                        log.append(f"[OpenCorePkg] WARNING: Drivers/{fname} has no counterpart in {tag}, left as-is")
            result["applied_parts"].append("drivers")

        if "resources" in parts:
            local_resources_dir = os.path.join(root, "Resources")
            new_resources_dir = os.path.join(oc_dir, "Resources")
            if os.path.isdir(new_resources_dir):
                if os.path.isdir(local_resources_dir):
                    shutil.rmtree(local_resources_dir)
                shutil.copytree(new_resources_dir, local_resources_dir)
                state["resources_theme"] = {"source": OPENCORE_REPO, "version": tag}
                log.append(f"[OpenCorePkg] Resources/ -> stock OpenCorePkg {tag}")
            result["applied_parts"].append("resources")

    save_state(root, state)
    return result


def apply_theme(repo, root, log, ref=None):
    """repo: 'owner/name' of a GitHub repo whose tree contains a Resources/
    folder in the shape OpenCanopy expects (Image/Label/Font/Audio)."""
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "theme.zip")
        log.append(f"[theme] downloading {repo} ({ref or 'default branch'}) ...")
        download_repo_archive(repo, zip_path, ref=ref)
        extract_dir = os.path.join(tmp, "extracted")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)

        resources_src = find_dir_named(extract_dir, "Resources")
        if not resources_src:
            raise RuntimeError(f"no Resources/ folder found anywhere in {repo}")

        local_resources_dir = os.path.join(root, "Resources")
        if os.path.isdir(local_resources_dir):
            shutil.rmtree(local_resources_dir)
        shutil.copytree(resources_src, local_resources_dir)
        log.append(f"[theme] Resources/ <- {repo}")

    state = load_state(root)
    state["resources_theme"] = {"source": repo, "ref": ref}
    save_state(root, state)
    return {"source": repo}


# ------------------------------------------------------- config migration --

def _merge_value(old_val, new_val, path, report):
    """Returns the merged value for one key path. `new_val` is the value
    from the NEW version's Sample.plist (i.e. the target schema/shape);
    `old_val` is what the user currently has."""
    if type(old_val) is not type(new_val) and not (
        isinstance(old_val, (int, float)) and isinstance(new_val, (int, float))
    ):
        report["type_mismatch"].append({"path": path, "old_type": type(old_val).__name__,
                                         "new_type": type(new_val).__name__})
        return new_val

    if isinstance(new_val, dict):
        result = {}
        for key, new_sub in new_val.items():
            sub_path = f"{path}.{key}" if path else key
            if key in old_val:
                result[key] = _merge_value(old_val[key], new_sub, sub_path, report)
                report["copied"].append(sub_path)
            else:
                result[key] = new_sub
                report["kept_new_default"].append(sub_path)
        for key in old_val:
            if key not in new_val:
                report["removed_in_new"].append(f"{path}.{key}" if path else key)
        return result

    if isinstance(new_val, list):
        # OpenCore arrays are lists of uniform-shaped dicts (Kernel->Add,
        # ACPI->Add, UEFI->Drivers, Booter->Patch, ...). Sample.plist ships
        # exactly one exemplar item - use it as the per-item template so
        # each of the user's real entries gets reconciled against the new
        # schema shape, key by key, instead of being replaced wholesale.
        # Plain user data (a hostlist path, an ACPI table filename, a
        # kext's own BundlePath/Enabled/Comment values) is preserved as-is;
        # only which KEYS each item has gets reconciled.
        if not new_val or not isinstance(new_val[0], dict):
            # scalar arrays (e.g. Add/Block name lists) - old data wins outright
            report["copied"].append(path + " (list, kept as-is)")
            return list(old_val)

        template = new_val[0]
        result = []
        for i, item in enumerate(old_val):
            if isinstance(item, dict):
                merged_item, _ = _merge_dict_against_template(item, template, f"{path}[{i}]", report)
                result.append(merged_item)
            else:
                result.append(item)
        report["copied"].append(f"{path} ({len(result)} item(s) carried over, reconciled against new schema)")
        return result

    # scalar leaf: old value always wins once type matches
    return old_val


def _merge_dict_against_template(old_item, template, path, report):
    result = {}
    for key, template_val in template.items():
        sub_path = f"{path}.{key}"
        if key in old_item:
            old_sub = old_item[key]
            if type(old_sub) is type(template_val) or (
                isinstance(old_sub, (int, float)) and isinstance(template_val, (int, float))
            ):
                result[key] = old_sub if not isinstance(template_val, (dict, list)) else \
                    _merge_value(old_sub, template_val, sub_path, report)
            else:
                report["type_mismatch"].append({"path": sub_path, "old_type": type(old_sub).__name__,
                                                 "new_type": type(template_val).__name__})
                result[key] = template_val
        else:
            result[key] = template_val
            report["kept_new_default"].append(sub_path)
    for key in old_item:
        if key not in template:
            # Not part of the new schema's template shape - most likely a
            # per-entry value that has no template counterpart to validate
            # against (rare). Keep it rather than silently drop user data.
            result[key] = old_item[key]
            report["removed_in_new"].append(f"{path}.{key} (kept anyway - not in new template)")
    return result, report


def migrate_config(old_config_path, new_sample_path):
    with open(old_config_path, "rb") as f:
        old_config = plistlib.load(f)
    with open(new_sample_path, "rb") as f:
        new_sample = plistlib.load(f)

    report = {"copied": [], "kept_new_default": [], "type_mismatch": [], "removed_in_new": []}
    merged = _merge_value(old_config, new_sample, "", report)
    if "" in [r for r in report["copied"] if r == ""]:
        pass  # top level path is empty string, harmless
    return merged, report
