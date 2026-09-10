#!/usr/bin/env python3
"""
Local browser UI for updating this EFI checkout. Pure stdlib (http.server),
no pip install needed. Run on the machine that has internet access and the
real EFI folder - not from a sandbox with no GitHub access.

    python3 server.py [--port 8765]

Then open http://127.0.0.1:8765 in a browser. Every destructive action
(update, migrate) requires the folder path to be typed into the UI
explicitly - nothing runs against a hardcoded path.
"""
import argparse
import json
import mimetypes
import os
import plistlib
import tempfile
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import core

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        fs_path = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not fs_path.startswith(STATIC_DIR) or not os.path.isfile(fs_path):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(fs_path)[0] or "application/octet-stream"
        with open(fs_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; errors still print via traceback below

    # ---------------------------------------------------------------- GET

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        try:
            if parsed.path == "/api/scan":
                root = qs.get("root", [""])[0]
                self._json(200, core.scan_root(root))
            elif parsed.path == "/api/check-updates":
                root = qs.get("root", [""])[0]
                channel = qs.get("channel", ["stable"])[0]
                self._json(200, core.check_updates(root, channel))
            else:
                self._serve_static(parsed.path)
        except Exception as e:
            traceback.print_exc()
            self._json(400, {"error": str(e)})

    # --------------------------------------------------------------- POST

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
            log = []

            if parsed.path == "/api/update-kext":
                result = core.apply_kext_component(body["component"], body["root"], body["channel"], log)
                self._json(200, {"result": result, "log": log})

            elif parsed.path == "/api/update-opencore":
                result = core.apply_opencore(body["root"], body["channel"], body["parts"], log)
                self._json(200, {"result": result, "log": log})

            elif parsed.path == "/api/apply-theme":
                result = core.apply_theme(body["repo"], body["root"], log, ref=body.get("ref"))
                self._json(200, {"result": result, "log": log})

            elif parsed.path == "/api/migrate-config":
                merged, report = self._migrate(body)
                self._json(200, {"report": report, "preview": _plist_preview(merged)})

            elif parsed.path == "/api/migrate-config/save":
                merged, report = self._migrate(body)
                out_path = os.path.join(body["root"], body.get("out_name", "config.migrated.plist"))
                with open(out_path, "wb") as f:
                    plistlib.dump(merged, f)
                self._json(200, {"saved_to": out_path, "report": report})

            else:
                self._json(404, {"error": "no such endpoint"})
        except Exception as e:
            traceback.print_exc()
            self._json(400, {"error": str(e)})

    def _migrate(self, body):
        """body: {root, new_opencore_zip_channel} - fetches the new
        version's Docs/Sample.plist straight from the OpenCorePkg release
        so the user doesn't have to hunt it down manually."""
        root = body["root"]
        old_config_path = os.path.join(root, "config.plist")
        channel = body.get("channel", "stable")

        tag, assets, _ = core.resolve_release(core.OPENCORE_REPO, channel)
        asset_name, asset_url = core.pick_asset(assets)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, asset_name)
            core.download(asset_url, zip_path)
            extract_dir = os.path.join(tmp, "extracted")
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(extract_dir)
            sample_path = core.find_in_tree(extract_dir, "Sample.plist")
            if not sample_path:
                raise RuntimeError(f"Docs/Sample.plist not found in {asset_name}")
            merged, report = core.migrate_config(old_config_path, sample_path)
        report["target_version"] = tag
        report["channel"] = channel
        return merged, report


def _plist_preview(d, max_len=20000):
    """A quick top-level summary for the browser, not the full plist (could
    be large / binary-heavy with ROM/UUID data)."""
    def summarize(v, depth=0):
        if depth > 2:
            return "..."
        if isinstance(v, dict):
            return {k: summarize(vv, depth + 1) for k, vv in v.items()}
        if isinstance(v, list):
            return f"[{len(v)} item(s)]"
        if isinstance(v, bytes):
            return f"<{len(v)} bytes>"
        return v
    text = json.dumps(summarize(d), indent=2, default=str)
    return text[:max_len]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"EFI updater running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
