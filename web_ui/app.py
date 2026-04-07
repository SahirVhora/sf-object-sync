"""
web_ui/app.py — Flask web interface for sf_object_sync.

Routes:
  GET  /                   — Upload / configuration form
  POST /process            — Handle file upload and start sync
  GET  /status/<run_id>    — Poll sync progress (JSON)
  GET  /download_template  — Serve sample input file
  GET  /results/<run_id>   — Show formatted results page
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from flask import (
    Flask, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from werkzeug.utils import secure_filename

# ── Path bootstrap so we can import src.* regardless of CWD ──────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

from src.sync_engine import sync_objects

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_DIR = os.path.abspath(os.path.join(_ROOT, "output"))
SAMPLE_FILE = os.path.join(_ROOT, "sample_data", "foundation_objects_template.xlsx")
ALLOWED_EXTENSIONS = {"xlsx", "csv"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# In-memory run registry: run_id → {"status": ..., "progress": ..., "result": ...}
_RUNS: Dict[str, Any] = {}
_RUNS_LOCK = threading.Lock()


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    # ── Validate upload ───────────────────────────────────────────────────────
    if "input_file" not in request.files or request.files["input_file"].filename == "":
        return render_template("index.html", error="No file selected."), 400

    f = request.files["input_file"]
    if not _allowed(f.filename):
        return render_template("index.html",
                               error="Invalid file type. Upload .xlsx or .csv."), 400

    filename = secure_filename(f.filename)
    run_id = uuid.uuid4().hex
    save_path = os.path.join(UPLOAD_FOLDER, f"{run_id}_{filename}")
    f.save(save_path)

    # ── Read form fields ──────────────────────────────────────────────────────
    auth_method = request.form.get("auth_method", "basic")
    # Checkbox is absent from form data when unchecked — treat presence as True
    dry_run = "dry_run" in request.form
    print(f"Dry Run Mode: {dry_run}")

    source_url = request.form.get("source_url", "").strip()
    source_user = request.form.get("source_user", "").strip()
    source_pass = request.form.get("source_password", "").strip()
    target_url = request.form.get("target_url", "").strip()
    target_user = request.form.get("target_user", "").strip()
    target_pass = request.form.get("target_password", "").strip()

    # OAuth 2.0 fields
    source_client_id     = request.form.get("source_client_id", "").strip()
    source_client_secret = request.form.get("source_client_secret", "").strip()
    source_token_url     = request.form.get("source_token_url", "").strip()
    target_client_id     = request.form.get("target_client_id", "").strip()
    target_client_secret = request.form.get("target_client_secret", "").strip()
    target_token_url     = request.form.get("target_token_url", "").strip()

    # Certificate fields
    source_cert_path   = request.form.get("source_cert_path", "").strip()
    source_key_path    = request.form.get("source_key_path", "").strip()
    source_company_id  = request.form.get("source_company_id", "").strip()
    target_cert_path   = request.form.get("target_cert_path", "").strip()
    target_key_path    = request.form.get("target_key_path", "").strip()
    target_company_id  = request.form.get("target_company_id", "").strip()

    # Fall back to env vars when form fields are blank
    source_url  = source_url  or os.getenv("SF_SOURCE_URL", "")
    source_user = source_user or os.getenv("SF_SOURCE_USER", os.getenv("SF_SOURCE_USERNAME", ""))
    source_pass = source_pass or os.getenv("SF_SOURCE_PASSWORD", "")
    target_url  = target_url  or os.getenv("SF_TARGET_URL", "")
    target_user = target_user or os.getenv("SF_TARGET_USER", os.getenv("SF_TARGET_USERNAME", ""))
    target_pass = target_pass or os.getenv("SF_TARGET_PASSWORD", "")

    if not source_url or not target_url:
        return render_template("index.html",
                               error="Source and Target URLs are required."), 400

    # Expose OAuth/cert credentials as env vars so auth_handler can pick them up
    os.environ["SF_SOURCE_CLIENT_ID"]     = source_client_id     or os.getenv("SF_SOURCE_CLIENT_ID", "")
    os.environ["SF_SOURCE_CLIENT_SECRET"] = source_client_secret or os.getenv("SF_SOURCE_CLIENT_SECRET", "")
    os.environ["SF_SOURCE_TOKEN_URL"]     = source_token_url     or os.getenv("SF_SOURCE_TOKEN_URL", "")
    os.environ["SF_SOURCE_CERT_PATH"]     = source_cert_path     or os.getenv("SF_SOURCE_CERT_PATH", "")
    os.environ["SF_SOURCE_KEY_PATH"]      = source_key_path      or os.getenv("SF_SOURCE_KEY_PATH", "")
    os.environ["SF_TARGET_CLIENT_ID"]     = target_client_id     or os.getenv("SF_TARGET_CLIENT_ID", "")
    os.environ["SF_TARGET_CLIENT_SECRET"] = target_client_secret or os.getenv("SF_TARGET_CLIENT_SECRET", "")
    os.environ["SF_TARGET_TOKEN_URL"]     = target_token_url     or os.getenv("SF_TARGET_TOKEN_URL", "")
    os.environ["SF_TARGET_CERT_PATH"]     = target_cert_path     or os.getenv("SF_TARGET_CERT_PATH", "")
    os.environ["SF_TARGET_KEY_PATH"]      = target_key_path      or os.getenv("SF_TARGET_KEY_PATH", "")

    source_config = {"base_url": source_url, "username": source_user, "password": source_pass}
    target_config = {"base_url": target_url, "username": target_user, "password": target_pass}

    # ── Register run and kick off background thread ───────────────────────────
    with _RUNS_LOCK:
        _RUNS[run_id] = {
            "status": "running",
            "phase": "init",
            "message": "Starting…",
            "percent": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "result": None,
        }

    os.environ["AUTH_METHOD"] = auth_method

    def _run():
        def _progress(phase: str, msg: str, pct: int) -> None:
            with _RUNS_LOCK:
                _RUNS[run_id].update({"phase": phase, "message": msg, "percent": pct})

        result = sync_objects(
            source_config=source_config,
            target_config=target_config,
            input_file_path=save_path,
            dry_run=dry_run,
            output_dir=OUTPUT_DIR,
            progress_callback=_progress,
        )

        with _RUNS_LOCK:
            _RUNS[run_id]["status"] = "done"
            _RUNS[run_id]["percent"] = 100
            _RUNS[run_id]["result"] = result
            _RUNS[run_id]["completed_at"] = datetime.now(timezone.utc).isoformat()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return redirect(url_for("status_page", run_id=run_id))


@app.route("/status/<run_id>")
def status_page(run_id: str):
    """Polling page — auto-redirects to results when done."""
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        return render_template("index.html", error=f"Unknown run ID: {run_id}"), 404
    if run["status"] == "done":
        return redirect(url_for("results_page", run_id=run_id))
    return render_template("status.html", run_id=run_id, run=run)


@app.route("/api/status/<run_id>")
def api_status(run_id: str):
    """JSON endpoint for polling from JavaScript."""
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        return jsonify({"error": "Unknown run ID"}), 404
    return jsonify({
        "status": run["status"],
        "phase": run.get("phase", ""),
        "message": run.get("message", ""),
        "percent": run.get("percent", 0),
    })


@app.route("/results/<run_id>")
def results_page(run_id: str):
    """Display sync results."""
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        return render_template("index.html", error=f"Unknown run ID: {run_id}"), 404
    if run["status"] != "done":
        return redirect(url_for("status_page", run_id=run_id))
    return render_template("results.html", run_id=run_id, run=run, result=run["result"])


@app.route("/download_template")
def download_template():
    """Serve the sample input xlsx."""
    if not os.path.isfile(SAMPLE_FILE):
        return "Sample template not found. Run: python sample_data/generate_template.py", 404
    return send_file(SAMPLE_FILE, as_attachment=True,
                     download_name="foundation_objects_template.xlsx")


@app.route("/download_report/<run_id>")
def download_report(run_id: str):
    """Download the Excel report for a completed run."""
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if run is None or run["status"] != "done":
        return "Run not found or not complete", 404
    report_path = (run.get("result") or {}).get("report_path")
    if not report_path or not os.path.isfile(report_path):
        return "Report not available", 404
    return send_file(report_path, as_attachment=True,
                     download_name=os.path.basename(report_path))


# ── Status page template (inline to keep file count low) ─────────────────────
# (We create a proper status.html below)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1")
    print(f"sf_object_sync web UI starting on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
