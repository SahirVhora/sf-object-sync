"""
sync_engine.py - programmatic API and CLI entry point for sf_object_sync.

Wraps the 7-phase sync pipeline so it can be called from:
  • The web UI (web_ui/app.py) via sync_objects()
  • The CLI:  python -m src.sync_engine --file sample.xlsx --dry-run

Migration from sf_object_sync.py
---------------------------------
Existing users can keep using sf_object_sync.py unchanged.  To migrate:
  1. Copy credentials to .env (see .env.example)
  2. Run:  python -m src.sync_engine --file input.xlsx --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Dotenv (optional) ─────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed - env vars must be set externally

# ── Local imports ─────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Imports below must follow the sys.path bootstrap so the 'src' package
# is resolvable when this file is run as a script (python src/sync_engine.py).
from src.config_loader import load_config  # noqa: E402
from src.entity_config import UPLOAD_ORDER, INPUT_VALID_TYPES, get_config  # noqa: E402
from src.sf_client import SFClient, SFClientError  # noqa: E402
from src.hierarchy_resolver import HierarchyResolver, HierarchyBrokenError, EntityNotFoundError  # noqa: E402
from src.gap_checker import GapChecker, DEV_MISSING, DEV_EXISTS  # noqa: E402
from src.payload_builder import build_payload, extract_parent_codes  # noqa: E402
from src.uploader import Uploader  # noqa: E402
from src.audit_logger import (  # noqa: E402
    AuditLogger,
    VALIDATION_FAILED, PRD_NOT_FOUND, HIERARCHY_BROKEN,
    DRY_RUN_OK, DRY_RUN_INTEGRITY_FAIL,
    UPLOAD_SUCCESS, UPLOAD_FAILED,
)
from src.report_generator import generate_report  # noqa: E402

logger = logging.getLogger("sync_engine")

ProgressCallback = Optional[Callable[[str, str, int], None]]
"""Signature: callback(phase_name, message, percent_complete)"""


def format_status_label(status_key: str) -> str:
    """Convert UPPER_CASE_STATUS to Title Case Status for display."""
    return " ".join(word.capitalize() for word in status_key.split("_"))


def _sf_error_message(exc: "SFClientError") -> str:  # noqa: F821
    """Return a human-friendly error message from an SFClientError."""
    code = exc.status_code
    if code == 401:
        return (
            "Authentication failed (HTTP 401 Unauthorized). "
            "Check your username/password or OAuth credentials in Settings."
        )
    if code == 403:
        return (
            "Access denied (HTTP 403 Forbidden). "
            "The API user lacks permission to read these entities. "
            "Check API role assignments in SuccessFactors."
        )
    if code == 404:
        return (
            f"OData endpoint not found (HTTP 404). "
            f"Verify the OData v2 Base URL is correct. Detail: {exc}"
        )
    if code is not None:
        return f"Source API returned HTTP {code}. Detail: {exc}"
    # status_code is None → network/connection error after retries
    return (
        f"Could not connect to the Source environment. "
        f"Check the OData Base URL and network access. Detail: {exc}"
    )


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(log_level: str, output_dir: str) -> str:
    """Configure root logger with file handler. Returns log file path."""
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(output_dir, f"sync_{ts}.log")

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )
    root.addHandler(fh)

    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers
               if not isinstance(h, logging.FileHandler)):
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(max(numeric_level, logging.INFO))
        ch.setFormatter(logging.Formatter("[%(levelname)s] %(name)s - %(message)s"))
        root.addHandler(ch)

    return log_path


# ── Config builders ───────────────────────────────────────────────────────────

def config_from_env() -> Dict[str, Any]:
    """
    Build a config dict from environment variables (dotenv-compatible).

    Equivalent to what load_config() returns from config.yaml, but reads
    from env vars instead.  Falls back to sensible defaults for options.
    """
    source_url = os.getenv("SF_SOURCE_URL", "")
    target_url = os.getenv("SF_TARGET_URL", "")

    if not source_url or not target_url:
        raise ValueError(
            "SF_SOURCE_URL and SF_TARGET_URL must be set in the environment "
            "or .env file when running without a config.yaml."
        )

    output_dir = os.path.abspath(os.getenv("OUTPUT_DIR", "./output"))
    os.makedirs(output_dir, exist_ok=True)

    return {
        "prd": {
            "base_url": source_url,
            "username": os.getenv("SF_SOURCE_USER", os.getenv("SF_SOURCE_USERNAME", "")),
            "password": os.getenv("SF_SOURCE_PASSWORD", ""),
        },
        "dev": {
            "base_url": target_url,
            "username": os.getenv("SF_TARGET_USER", os.getenv("SF_TARGET_USERNAME", "")),
            "password": os.getenv("SF_TARGET_PASSWORD", ""),
        },
        "options": {
            "dry_run": os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
            "log_level": os.getenv("LOG_LEVEL", "INFO"),
            "output_dir": output_dir,
            "request_timeout_sec": int(os.getenv("REQUEST_TIMEOUT_SEC", "30")),
            "locales_to_sync": ["defaultValue", "en_US", "en_GB"],
        },
    }


def _make_clients(cfg: Dict[str, Any]) -> Tuple[SFClient, SFClient]:
    """Build PRD + Dev SFClient from a config dict."""
    auth_method = os.getenv("AUTH_METHOD", "basic").lower()

    if auth_method == "basic":
        prd = SFClient(
            cfg["prd"]["base_url"],
            cfg["prd"]["username"],
            cfg["prd"]["password"],
            timeout_sec=cfg["options"].get("request_timeout_sec", 30),
        )
        dev = SFClient(
            cfg["dev"]["base_url"],
            cfg["dev"]["username"],
            cfg["dev"]["password"],
            timeout_sec=cfg["options"].get("request_timeout_sec", 30),
        )
    else:
        from src.auth_handler import build_sf_client
        timeout = cfg["options"].get("request_timeout_sec", 30)
        prd = build_sf_client("source", cfg["prd"]["base_url"], timeout_sec=timeout)
        dev = build_sf_client("target", cfg["dev"]["base_url"], timeout_sec=timeout)

    return prd, dev


# ── Phase helpers (re-exported from sf_object_sync phases) ───────────────────
# These are thin wrappers that add progress_callback support without touching
# the original sf_object_sync.py logic.

def _emit(cb: ProgressCallback, phase: str, msg: str, pct: int) -> None:
    if cb:
        try:
            cb(phase, msg, pct)
        except Exception:
            pass


def _phase1_validate(input_path: str, audit: AuditLogger, errors: list) -> list:
    """Validate input xlsx; returns valid rows."""
    import openpyxl

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    wb = openpyxl.load_workbook(input_path, read_only=True, data_only=True)
    rows_iter = None
    header_row = None
    header_row_number = 0
    for candidate in wb.worksheets:
        candidate_rows = iter(candidate.rows)
        for row_number, row in enumerate(candidate_rows, start=1):
            vals = [str(c.value or "").strip().lower() for c in row]
            if "object" in vals and "code" in vals:
                rows_iter = candidate_rows
                header_row = vals
                header_row_number = row_number
                break
        if header_row is not None:
            break

    if header_row is None:
        raise ValueError("Input file missing required header row with 'Object' and 'Code' columns")

    obj_idx = header_row.index("object")
    code_idx = header_row.index("code")

    valid_rows: list = []
    row_number = header_row_number

    for row in rows_iter:
        row_number += 1
        try:
            raw_object = str(row[obj_idx].value or "").strip()
            raw_code = str(row[code_idx].value or "").strip()
        except IndexError:
            continue

        if not raw_object and not raw_code:
            continue

        ts = datetime.now(timezone.utc).isoformat()
        clean = raw_object.strip()
        object_type = next(
            (vt for vt in INPUT_VALID_TYPES if clean.lower() == vt.lower()), None
        )

        if object_type is None:
            reason = f"Invalid Object type '{raw_object}'. Must be one of: {sorted(INPUT_VALID_TYPES)}"
            audit.log(phase="validation", status=VALIDATION_FAILED,
                      object_type=raw_object, external_code=raw_code, error_message=reason)
            errors.append({"row_number": row_number, "input_object": raw_object,
                           "input_code": raw_code, "reason": reason, "timestamp": ts})
            continue

        if not raw_code:
            reason = "Code is empty"
            audit.log(phase="validation", status=VALIDATION_FAILED,
                      object_type=object_type, external_code="", error_message=reason)
            errors.append({"row_number": row_number, "input_object": raw_object,
                           "input_code": "", "reason": reason, "timestamp": ts})
            continue

        if not raw_code.replace("-", "").replace("_", "").isalnum():
            reason = f"Code '{raw_code}' contains invalid characters"
            audit.log(phase="validation", status=VALIDATION_FAILED,
                      object_type=object_type, external_code=raw_code, error_message=reason)
            errors.append({"row_number": row_number, "input_object": raw_object,
                           "input_code": raw_code, "reason": reason, "timestamp": ts})
            continue

        valid_rows.append({"object_type": object_type, "code": raw_code})

    wb.close()
    return valid_rows


def _phase2_prd_check(valid_rows, prd_client, audit, prd_cache):
    from src.hierarchy_resolver import _select_active_record
    confirmed = []
    for row in valid_rows:
        entity_type, code = row["object_type"], row["code"]
        cfg = get_config(entity_type)
        parent_nav = cfg.get("parent_nav")
        try:
            records = prd_client.get_entity_by_code(
                cfg["entity_set"], code, expand=parent_nav
            )
        except SFClientError as exc:
            # Auth failures and connection errors affect every row - surface them
            # immediately rather than silently treating all rows as PRD_NOT_FOUND.
            if exc.status_code in (401, 403) or exc.status_code is None:
                raise
            audit.log(phase="prd_check", status=PRD_NOT_FOUND,
                      object_type=entity_type, entity_set=cfg["entity_set"],
                      external_code=code, error_message=str(exc))
            continue

        if not records:
            audit.log(phase="prd_check", status=PRD_NOT_FOUND,
                      object_type=entity_type, entity_set=cfg["entity_set"],
                      external_code=code, error_message="No records returned from PRD")
            continue

        active = _select_active_record(records, entity_type, code)
        if active is None:
            audit.log(phase="prd_check", status=PRD_NOT_FOUND,
                      object_type=entity_type, entity_set=cfg["entity_set"],
                      external_code=code,
                      error_message="No active (open-ended) record found in PRD")
            continue

        prd_cache[(entity_type, code)] = active
        confirmed.append(row)

    return confirmed


def _phase3_resolve_hierarchies(confirmed_rows, prd_client, audit, prd_cache):
    resolver = HierarchyResolver(prd_client)
    for (etype, code), record in list(prd_cache.items()):
        resolver.prime_cache(etype, code, record)

    resolved_chains, failed_rows = [], []
    for row in confirmed_rows:
        entity_type, code = row["object_type"], row["code"]
        cfg = get_config(entity_type)
        try:
            chain = resolver.resolve(entity_type, code)
            resolved_chains.append(chain)
        except HierarchyBrokenError as exc:
            audit.log(phase="hierarchy", status=HIERARCHY_BROKEN,
                      object_type=entity_type, entity_set=cfg["entity_set"],
                      external_code=code, error_message=str(exc))
            failed_rows.append(row)
        except EntityNotFoundError as exc:
            audit.log(phase="hierarchy", status=PRD_NOT_FOUND,
                      object_type=exc.entity_type,
                      entity_set=get_config(exc.entity_type)["entity_set"],
                      external_code=exc.code, error_message=str(exc))
            failed_rows.append(row)

    prd_cache.update(resolver.get_cache())
    return resolved_chains, failed_rows


def _phase4_gap_analysis(resolved_chains, dev_client, audit):
    checker = GapChecker(dev_client)
    # Track which entities have already been logged so that shared parent entities
    # appearing in multiple chains are only counted once in the audit.
    logged: set = set()
    for chain in resolved_chains:
        for result in checker.check_chain(chain):
            key = (result.entity_type, result.external_code)
            if key in logged:
                continue  # already logged from an earlier chain - skip duplicate
            logged.add(key)
            audit.log(phase="gap_check", status=result.status,
                      object_type=result.entity_type, entity_set=result.entity_set,
                      external_code=result.external_code)
    return checker, checker.get_results()


def _phase5_dry_run(resolved_chains, gap_results, prd_cache, audit, output_dir,
                    dev_base_url=""):
    import json
    lines = [
        "=" * 70,
        "  DRY RUN SUMMARY - sf_object_sync",
        f"  Generated at: {datetime.now(timezone.utc).isoformat()}",
        "=" * 70, "",
    ]

    missing_by_level = {}
    for chain in resolved_chains:
        for etype, code, record in chain:
            gap = gap_results.get((etype, code))
            if gap and gap.status == DEV_MISSING:
                missing_by_level.setdefault(etype, [])
                if not any(c == code for _, c, _ in missing_by_level[etype]):
                    missing_by_level[etype].append((etype, code, record))

    total_missing = sum(len(v) for v in missing_by_level.values())
    lines += [f"  Total entities to create: {total_missing}", ""]

    for etype in UPLOAD_ORDER:
        entities = missing_by_level.get(etype, [])
        if not entities:
            continue
        lines.append(f"── {etype} ({get_config(etype)['entity_set']}) ──")
        lines.append(f"   Count: {len(entities)}")
        lines.append("")
        for _, code, record in entities:
            parent_codes = {}
            for chain in resolved_chains:
                for ce, cc, _ in chain:
                    if ce == etype and cc == code:
                        parent_codes = extract_parent_codes(chain)
                        break
                if parent_codes:
                    break
            if dev_base_url:
                parent_codes["_base_url"] = dev_base_url
            try:
                payload = build_payload(etype, record, parent_codes)
                payload_str = json.dumps(payload, indent=2)
                status = DRY_RUN_OK
            except Exception as exc:
                payload_str = f"ERROR: {exc}"
                status = DRY_RUN_INTEGRITY_FAIL
            audit.log(phase="dry_run", status=status,
                      object_type=etype, entity_set=get_config(etype)["entity_set"],
                      external_code=code,
                      payload_sent=payload if status == DRY_RUN_OK else None,
                      error_message="" if status == DRY_RUN_OK else payload_str)
            lines += [f"   externalCode: {code}", "   Payload:"]
            lines += [f"     {line}" for line in payload_str.splitlines()]
            lines.append("")

    if not missing_by_level:
        lines.append("  All entities already exist in Dev. Nothing to create.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    os.makedirs(output_dir, exist_ok=True)
    summary_path = os.path.join(output_dir, f"dry_run_summary_{ts}.txt")
    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    return summary_path, total_missing


def _phase6_upload(resolved_chains, gap_results, prd_cache, dev_client, audit):
    uploader = Uploader(
        dev_client=dev_client,
        prd_records=prd_cache,
        gap_results=gap_results,
        audit=audit,
        dry_run=False,
    )
    uploader.run(resolved_chains)


def _phase7_report(audit, run_meta, resolved_chains, gap_results, valid_rows,
                   validation_errors, output_dir):
    seen: set = set()
    object_detail_rows = []
    for chain in resolved_chains:
        for etype, code, record in chain:
            if (etype, code) in seen:
                continue
            seen.add((etype, code))
            cfg = get_config(etype)
            gap = gap_results.get((etype, code))
            dev_status = gap.status if gap else "UNKNOWN"
            upload_records = [
                r for r in audit.all_records()
                if r.get("external_code") == code and r.get("object_type") == etype
                and r.get("phase") == "upload"
            ]
            upload_status = (upload_records[-1]["status"] if upload_records else
                             ("DRY_RUN" if run_meta.get("dry_run") else "NOT_PROCESSED"))
            error = upload_records[-1].get("error_message", "") if upload_records else ""
            object_detail_rows.append({
                "input_object": etype, "input_code": code, "level": cfg["level"],
                "entity_set": cfg["entity_set"], "external_code": code,
                "prd_status": "FOUND", "dev_status_before": dev_status,
                "action_taken": "CREATE" if dev_status == DEV_MISSING else "SKIP",
                "upload_status": upload_status, "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    return generate_report(
        output_dir=output_dir, run_meta=run_meta,
        audit_records=audit.all_records(),
        object_detail_rows=object_detail_rows,
        validation_errors=validation_errors,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def sync_objects(
    source_config: Dict[str, Any],
    target_config: Dict[str, Any],
    input_file_path: str,
    dry_run: bool = True,
    output_dir: str = "./output",
    log_level: str = "INFO",
    timeout_sec: int = 30,
    progress_callback: ProgressCallback = None,
) -> Dict[str, Any]:
    """
    Synchronise SuccessFactors OM foundation objects from source to target.

    Args:
        source_config   : {"base_url": str, "username": str, "password": str}
        target_config   : {"base_url": str, "username": str, "password": str}
        input_file_path : path to xlsx with Object / Code columns
        dry_run         : when True, no writes are made to target
        output_dir      : directory for logs and reports
        log_level       : Python logging level string
        timeout_sec     : per-request HTTP timeout
        progress_callback : optional callable(phase, message, pct_int)

    Returns:
        {
            "success":  int,   # entities successfully created
            "failure":  int,   # entities that failed
            "skipped":  int,   # already exist in target
            "dry_run":  bool,
            "logs":     list[str],  # human-readable log lines
            "report_path": str | None,
            "summary_path": str | None,
            "error":    str | None,  # set on fatal error
        }
    """
    os.makedirs(output_dir, exist_ok=True)
    setup_logging(log_level, output_dir)
    log_lines: List[str] = []

    def _log(msg: str) -> None:
        log_lines.append(msg)
        logger.info(msg)

    assert isinstance(dry_run, bool), f"dry_run must be bool, got {type(dry_run)}"

    result: Dict[str, Any] = {
        "success": 0, "failure": 0, "skipped": 0,
        "dry_run": dry_run, "logs": log_lines,
        "report_path": None, "summary_path": None,
        "status_counts": {}, "error": None,
    }

    _emit(progress_callback, "init", "Starting sync", 0)
    logger.info("=== RUNNING IN %s MODE ===", "DRY RUN" if dry_run else "LIVE")
    started_at = datetime.now(timezone.utc).isoformat()

    full_cfg: Dict[str, Any] = {
        "prd": source_config,
        "dev": target_config,
        "options": {
            "dry_run": dry_run,
            "output_dir": output_dir,
            "log_level": log_level,
            "request_timeout_sec": timeout_sec,
            "locales_to_sync": ["defaultValue", "en_US", "en_GB"],
        },
    }

    prd_cache: Dict[Tuple, Any] = {}
    validation_errors: List[Dict] = []
    resolved_chains: List = []
    gap_results: Dict = {}

    run_meta: Dict[str, Any] = {
        "started_at": started_at,
        "input_file": os.path.abspath(input_file_path),
        "dry_run": dry_run,
        "prd_url": source_config.get("base_url", ""),
        "dev_url": target_config.get("base_url", ""),
    }

    try:
        prd_client, dev_client = _make_clients(full_cfg)
    except Exception as exc:
        result["error"] = f"Failed to build API clients: {exc}"
        return result

    with AuditLogger(output_dir, dry_run) as audit:
        run_meta["run_id"] = audit.run_id

        try:
            # Phase 1
            _emit(progress_callback, "validation", "Validating input file", 10)
            _log(f"Phase 1 - Validating input: {input_file_path}")
            valid_rows = _phase1_validate(input_file_path, audit, validation_errors)
            run_meta.update({
                "total_rows": len(valid_rows) + len(validation_errors),
                "valid_rows": len(valid_rows),
            })
            _log(f"Phase 1 complete: {len(valid_rows)} valid, {len(validation_errors)} rejected")

            if not valid_rows:
                result["error"] = "No valid rows found in input file"
                return result

            # Phase 2
            _emit(progress_callback, "prd_check", "Checking objects in PRD", 25)
            _log(f"Phase 2 - PRD existence check ({len(valid_rows)} rows)")
            confirmed = _phase2_prd_check(valid_rows, prd_client, audit, prd_cache)
            _log(f"Phase 2 complete: {len(confirmed)} confirmed in PRD")

            if not confirmed:
                result["error"] = "No rows confirmed in PRD"
                return result

            # Phase 3
            _emit(progress_callback, "hierarchy", "Resolving parent hierarchies", 40)
            _log("Phase 3 - Hierarchy traversal")
            resolved_chains, _ = _phase3_resolve_hierarchies(
                confirmed, prd_client, audit, prd_cache
            )
            _log(f"Phase 3 complete: {len(resolved_chains)} chains resolved")

            if not resolved_chains:
                result["error"] = "No hierarchies could be resolved"
                return result

            # Phase 4
            _emit(progress_callback, "gap_analysis", "Checking Dev for gaps", 55)
            _log("Phase 4 - Dev gap analysis")
            checker, gap_results = _phase4_gap_analysis(resolved_chains, dev_client, audit)

            missing_count = sum(1 for r in gap_results.values() if r.status == DEV_MISSING)
            skipped_count = sum(1 for r in gap_results.values() if r.status == DEV_EXISTS)
            result["skipped"] = skipped_count
            _log(f"Phase 4 complete: {missing_count} missing, {skipped_count} exist in Dev")

            # Phase 5 or 6
            if dry_run:
                _emit(progress_callback, "dry_run", "Building dry-run payloads", 70)
                _log("Phase 5 - Dry run (no writes)")
                summary_path, _ = _phase5_dry_run(
                    resolved_chains, gap_results, prd_cache, audit,
                    output_dir, dev_base_url=dev_client.base_url,
                )
                result["summary_path"] = summary_path
                result["success"] = missing_count  # "would create"
                _log(f"Dry-run summary: {summary_path}")
            else:
                _emit(progress_callback, "upload", "Uploading to Dev", 70)
                _log("Phase 6 - Live upload")
                _phase6_upload(resolved_chains, gap_results, prd_cache, dev_client, audit)

                # Count outcomes from audit
                upload_records = [
                    r for r in audit.all_records() if r.get("phase") == "upload"
                ]
                result["success"] = sum(
                    1 for r in upload_records if r.get("status") == UPLOAD_SUCCESS
                )
                result["failure"] = sum(
                    1 for r in upload_records if r.get("status") == UPLOAD_FAILED
                )

        except SFClientError as exc:
            result["error"] = _sf_error_message(exc)
            logger.error(result["error"])
        except FileNotFoundError as exc:
            result["error"] = str(exc)
            logger.error(str(exc))
        except ValueError as exc:
            result["error"] = str(exc)
            logger.error(str(exc))
        except Exception as exc:
            result["error"] = f"Unexpected error: {exc}"
            logger.error(traceback.format_exc())
        finally:
            prd_client.close()
            dev_client.close()

        # Phase 7 - report (always attempt, even on partial failure)
        _emit(progress_callback, "report", "Generating Excel report", 90)
        run_meta["completed_at"] = datetime.now(timezone.utc).isoformat()
        try:
            report_path = _phase7_report(
                audit=audit, run_meta=run_meta,
                resolved_chains=resolved_chains, gap_results=gap_results,
                valid_rows=valid_rows if "valid_rows" in dir() else [],
                validation_errors=validation_errors,
                output_dir=output_dir,
            )
            result["report_path"] = report_path
            _log(f"Excel report: {report_path}")
        except Exception as exc:
            logger.error("Report generation failed: %s", exc)

        # Collect status counts from all audit records
        raw_counts: Dict[str, int] = {}
        for rec in audit.all_records():
            s = rec.get("status", "UNKNOWN")
            raw_counts[s] = raw_counts.get(s, 0) + 1
        result["status_counts"] = {
            format_status_label(k): v for k, v in raw_counts.items()
        }

    _emit(progress_callback, "done", "Sync complete", 100)
    result["logs"] = log_lines
    return result


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.sync_engine",
        description="Sync SAP SuccessFactors OM foundation objects from source to target.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (can be set in .env):
  SF_SOURCE_URL, SF_SOURCE_USER, SF_SOURCE_PASSWORD
  SF_TARGET_URL, SF_TARGET_USER, SF_TARGET_PASSWORD
  AUTH_METHOD  (basic | oauth | certificate)

Examples:
  # Env-based credentials, dry run:
  python -m src.sync_engine --file sample.xlsx --dry-run

  # Config-file credentials, live upload, verbose:
  python -m src.sync_engine --config config.yaml --file input.xlsx --verbose

  # Explicit env overrides:
  SF_SOURCE_URL=https://prd.example.com/odata/v2 python -m src.sync_engine --file input.xlsx
        """,
    )
    parser.add_argument("--file", required=True, metavar="PATH",
                        help="Path to input xlsx (columns: Object, Code)")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="Optional config.yaml (overrides env var credentials)")
    parser.add_argument("--source-env", default=None, metavar="LABEL",
                        help="Human label for source env (informational)")
    parser.add_argument("--target-env", default=None, metavar="LABEL",
                        help="Human label for target env (informational)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Preview only - no writes to target (default: from env/config)")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="Output directory for logs/reports (default: ./output)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Set log level to DEBUG")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.config:
        try:
            cfg = load_config(args.config)
        except (FileNotFoundError, ValueError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        source_cfg = {"base_url": cfg["prd"]["base_url"],
                      "username": cfg["prd"]["username"],
                      "password": cfg["prd"]["password"]}
        target_cfg = {"base_url": cfg["dev"]["base_url"],
                      "username": cfg["dev"]["username"],
                      "password": cfg["dev"]["password"]}
        output_dir = cfg["options"]["output_dir"]
        dry_run = args.dry_run or cfg["options"].get("dry_run", True)
        timeout = cfg["options"].get("request_timeout_sec", 30)
    else:
        try:
            env_cfg = config_from_env()
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        source_cfg = env_cfg["prd"]
        target_cfg = env_cfg["dev"]
        output_dir = env_cfg["options"]["output_dir"]
        dry_run = args.dry_run or env_cfg["options"]["dry_run"]
        timeout = env_cfg["options"]["request_timeout_sec"]

    if args.output_dir:
        output_dir = os.path.abspath(args.output_dir)

    log_level = "DEBUG" if args.verbose else "INFO"

    def _cli_progress(phase: str, msg: str, pct: int) -> None:
        print(f"[{pct:>3}%] {phase}: {msg}")

    result = sync_objects(
        source_config=source_cfg,
        target_config=target_cfg,
        input_file_path=args.file,
        dry_run=dry_run,
        output_dir=output_dir,
        log_level=log_level,
        timeout_sec=timeout,
        progress_callback=_cli_progress,
    )

    if result["error"]:
        print(f"\n[ERROR] {result['error']}", file=sys.stderr)
        return 1

    mode = "DRY RUN" if result["dry_run"] else "LIVE"
    print(f"\n── Sync complete ({mode}) ──────────────────────────")
    print(f"  Would create / Created : {result['success']}")
    print(f"  Already exist (skipped): {result['skipped']}")
    print(f"  Failed                 : {result['failure']}")
    if result["report_path"]:
        print(f"  Excel report           : {result['report_path']}")
    if result["summary_path"]:
        print(f"  Dry-run summary        : {result['summary_path']}")
    print()

    return 0 if not result["failure"] else 1


if __name__ == "__main__":
    sys.exit(main())
