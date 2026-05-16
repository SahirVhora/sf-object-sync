#!/usr/bin/env python3
"""
sf_object_sync.py - SAP SuccessFactors OM Foundation Object Sync Tool

Synchronises Sub Departments and Departments (+ full parent chains) from a
PRD SuccessFactors tenant to a Dev tenant via OData v2 Basic Auth APIs.

Usage:
    python sf_object_sync.py --config config.yaml --input input.xlsx
    python sf_object_sync.py --config config.yaml --input input.xlsx --dry-run
    python sf_object_sync.py --config config.yaml --input input.xlsx --verbose
"""

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from colorama import Fore, Style, init as colorama_init

# ── Local imports ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from src.config_loader import load_config
from src.entity_config import ENTITY_CONFIG, UPLOAD_ORDER, INPUT_VALID_TYPES, get_config
from src.sf_client import SFClient, SFClientError
from src.hierarchy_resolver import HierarchyResolver, HierarchyBrokenError, EntityNotFoundError
from src.gap_checker import GapChecker, DEV_MISSING, DEV_EXISTS
from src.payload_builder import build_payload, extract_parent_codes
from src.uploader import Uploader
from src.audit_logger import (
    AuditLogger,
    VALIDATION_FAILED, PRD_NOT_FOUND, HIERARCHY_BROKEN,
    DRY_RUN_OK, DRY_RUN_INTEGRITY_FAIL,
    UPLOAD_SUCCESS, UPLOAD_FAILED,
)
from src.report_generator import generate_report

colorama_init(autoreset=True)

# ── Coloured logging handler ─────────────────────────────────────────────────

class ColouredConsoleHandler(logging.StreamHandler):
    LEVEL_COLOURS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.WHITE,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.MAGENTA,
    }

    def emit(self, record: logging.LogRecord) -> None:
        colour = self.LEVEL_COLOURS.get(record.levelno, "")
        record.msg = colour + str(record.msg) + Style.RESET_ALL
        super().emit(record)


def setup_logging(log_level: str, output_dir: str) -> str:
    """Configure root logger with coloured console + file handler."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(output_dir, f"run_{ts}.log")

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Console (coloured) - always INFO or above; DEBUG goes to file only
    ch = ColouredConsoleHandler(sys.stdout)
    ch.setLevel(max(numeric_level, logging.INFO))
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(name)s - %(message)s"))
    root.addHandler(ch)

    # File (plain)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)  # always capture everything in file
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )
    root.addHandler(fh)

    return log_path


logger = logging.getLogger("sf_object_sync")


# ── Phase 1: Input Validation ────────────────────────────────────────────────

def _normalise_object_type(raw: str) -> Optional[str]:
    """Case-insensitive match against INPUT_VALID_TYPES."""
    clean = raw.strip()
    for vtype in INPUT_VALID_TYPES:
        if clean.lower() == vtype.lower():
            return vtype
    return None


def phase1_validate_input(
    input_path: str,
    audit: AuditLogger,
    validation_errors: list,
) -> List[Dict[str, str]]:
    """
    Read input.xlsx and validate each row.

    Returns list of valid rows: [{"object_type": ..., "code": ...}, ...]
    """
    logger.info("Phase 1 - Validating input: %s", input_path)

    if not os.path.isfile(input_path):
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    try:
        wb = openpyxl.load_workbook(input_path, read_only=True, data_only=True)
    except Exception as exc:
        logger.error("Failed to open input file: %s", exc)
        sys.exit(1)

    ws = wb.active
    rows_iter = iter(ws.rows)

    # Find header row (first non-empty row)
    header_row = None
    for row in rows_iter:
        vals = [str(c.value or "").strip().lower() for c in row]
        if "object" in vals and "code" in vals:
            header_row = vals
            break

    if header_row is None:
        logger.error("Input file missing required header row with 'Object' and 'Code' columns")
        sys.exit(1)

    try:
        obj_idx = header_row.index("object")
        code_idx = header_row.index("code")
    except ValueError:
        logger.error("Could not find 'Object' or 'Code' column in header row")
        sys.exit(1)

    valid_rows: List[Dict[str, str]] = []
    row_number = 1  # header is row 1

    for row in rows_iter:
        row_number += 1
        try:
            raw_object = str(row[obj_idx].value or "").strip()
            raw_code = str(row[code_idx].value or "").strip()
        except IndexError:
            continue

        if not raw_object and not raw_code:
            continue  # skip blank rows

        ts = datetime.now(timezone.utc).isoformat()

        # Validate Object
        object_type = _normalise_object_type(raw_object)
        if object_type is None:
            reason = (
                f"Invalid Object type '{raw_object}'. "
                f"Must be one of: {sorted(INPUT_VALID_TYPES)}"
            )
            logger.warning("Row %d: %s", row_number, reason)
            audit.log(
                phase="validation",
                status=VALIDATION_FAILED,
                object_type=raw_object,
                external_code=raw_code,
                error_message=reason,
            )
            validation_errors.append({
                "row_number": row_number,
                "input_object": raw_object,
                "input_code": raw_code,
                "reason": reason,
                "timestamp": ts,
            })
            continue

        # Validate Code
        if not raw_code:
            reason = "Code is empty"
            logger.warning("Row %d: %s", row_number, reason)
            audit.log(
                phase="validation",
                status=VALIDATION_FAILED,
                object_type=object_type,
                external_code="",
                error_message=reason,
            )
            validation_errors.append({
                "row_number": row_number,
                "input_object": raw_object,
                "input_code": "",
                "reason": reason,
                "timestamp": ts,
            })
            continue

        if not raw_code.replace("-", "").replace("_", "").isalnum():
            reason = f"Code '{raw_code}' contains invalid characters (must be alphanumeric)"
            logger.warning("Row %d: %s", row_number, reason)
            audit.log(
                phase="validation",
                status=VALIDATION_FAILED,
                object_type=object_type,
                external_code=raw_code,
                error_message=reason,
            )
            validation_errors.append({
                "row_number": row_number,
                "input_object": raw_object,
                "input_code": raw_code,
                "reason": reason,
                "timestamp": ts,
            })
            continue

        valid_rows.append({"object_type": object_type, "code": raw_code})

    wb.close()

    # Summary table
    print(f"\n{'─'*60}")
    print(f"  INPUT VALIDATION SUMMARY")
    print(f"{'─'*60}")
    print(f"  Total data rows read   : {row_number - 1}")
    print(f"  Valid rows             : {len(valid_rows)}")
    print(f"  Rejected rows          : {len(validation_errors)}")
    if valid_rows:
        print(f"\n  Valid rows to process:")
        print(f"  {'#':<5} {'Object Type':<20} {'Code'}")
        print(f"  {'─'*45}")
        for i, row in enumerate(valid_rows, 1):
            print(f"  {i:<5} {row['object_type']:<20} {row['code']}")
    print()

    if not valid_rows:
        logger.error("No valid rows to process. Aborting.")
        sys.exit(1)

    return valid_rows


# ── Phase 2: PRD Existence Check ─────────────────────────────────────────────

def phase2_prd_check(
    valid_rows: List[Dict[str, str]],
    prd_client: SFClient,
    audit: AuditLogger,
    prd_cache: Dict[Tuple[str, str], Any],
) -> List[Dict[str, str]]:
    """
    Verify each input row exists in PRD.

    Populates prd_cache with (entity_type, code) → active record.
    Returns list of rows confirmed in PRD.
    """
    logger.info("Phase 2 - PRD existence check (%d rows)", len(valid_rows))
    confirmed: List[Dict[str, str]] = []

    from src.hierarchy_resolver import _select_active_record

    for row in valid_rows:
        entity_type = row["object_type"]
        code = row["code"]
        cfg = get_config(entity_type)
        entity_set = cfg["entity_set"]

        # Fetch with $expand on parent nav so the cached record already
        # contains the embedded parent - Phase 3 cache hits will have it.
        parent_nav = cfg.get("parent_nav")
        logger.info(
            "PRD check: %s '%s' (%s)%s",
            entity_type, code, entity_set,
            f" [$expand={parent_nav}]" if parent_nav else "",
        )

        try:
            records = prd_client.get_entity_by_code(entity_set, code, expand=parent_nav)
        except SFClientError as exc:
            logger.error("PRD API error for %s '%s': %s", entity_type, code, exc)
            audit.log(
                phase="prd_check",
                status=PRD_NOT_FOUND,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                error_message=str(exc),
            )
            continue

        if not records:
            logger.warning("PRD_NOT_FOUND: %s '%s'", entity_type, code)
            audit.log(
                phase="prd_check",
                status=PRD_NOT_FOUND,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                error_message="No records returned from PRD",
            )
            continue

        active = _select_active_record(records, entity_type, code)
        if active is None:
            logger.warning("PRD_NOT_FOUND (no active record): %s '%s'", entity_type, code)
            audit.log(
                phase="prd_check",
                status=PRD_NOT_FOUND,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                error_message="No active (open-ended) record found in PRD",
            )
            continue

        prd_cache[(entity_type, code)] = active
        confirmed.append(row)
        logger.info("PRD confirmed: %s '%s'", entity_type, code)

    logger.info(
        "Phase 2 complete: %d confirmed, %d not found",
        len(confirmed),
        len(valid_rows) - len(confirmed),
    )
    return confirmed


# ── Phase 3: Hierarchy Traversal ─────────────────────────────────────────────

def phase3_resolve_hierarchies(
    confirmed_rows: List[Dict[str, str]],
    prd_client: SFClient,
    audit: AuditLogger,
    prd_cache: Dict[Tuple[str, str], Any],
) -> Tuple[List[List[Tuple[str, str, Dict]]], List[Dict[str, str]]]:
    """
    Resolve the full parent chain for each confirmed row.

    Populates prd_cache from HierarchyResolver's internal cache.
    Returns (resolved_chains, failed_rows).
    """
    logger.info("Phase 3 - Hierarchy traversal (%d rows)", len(confirmed_rows))
    resolver = HierarchyResolver(prd_client)

    # Pre-populate resolver cache from Phase 2 fetches
    for (etype, code), record in list(prd_cache.items()):
        resolver.prime_cache(etype, code, record)

    resolved_chains: List[List[Tuple[str, str, Dict]]] = []
    failed_rows: List[Dict[str, str]] = []

    for row in confirmed_rows:
        entity_type = row["object_type"]
        code = row["code"]
        cfg = get_config(entity_type)
        entity_set = cfg["entity_set"]

        try:
            chain = resolver.resolve(entity_type, code)
            resolved_chains.append(chain)
            logger.info(
                "Hierarchy resolved: %s '%s' → %d levels",
                entity_type,
                code,
                len(chain),
            )
            for etype, ecode, _ in chain:
                logger.debug("  chain member: %s '%s'", etype, ecode)

        except HierarchyBrokenError as exc:
            logger.error(str(exc))
            audit.log(
                phase="hierarchy",
                status=HIERARCHY_BROKEN,
                object_type=entity_type,
                entity_set=entity_set,
                external_code=code,
                error_message=str(exc),
            )
            failed_rows.append(row)

        except EntityNotFoundError as exc:
            logger.error(str(exc))
            audit.log(
                phase="hierarchy",
                status=PRD_NOT_FOUND,
                object_type=exc.entity_type,
                entity_set=get_config(exc.entity_type)["entity_set"],
                external_code=exc.code,
                error_message=str(exc),
            )
            failed_rows.append(row)

    # Merge resolver cache back into shared prd_cache
    prd_cache.update(resolver.get_cache())

    logger.info(
        "Phase 3 complete: %d chains resolved, %d failed",
        len(resolved_chains),
        len(failed_rows),
    )
    return resolved_chains, failed_rows


# ── Phase 4: Dev Gap Analysis ────────────────────────────────────────────────

def phase4_gap_analysis(
    resolved_chains: List[List[Tuple[str, str, Dict]]],
    dev_client: SFClient,
    audit: AuditLogger,
) -> Tuple[GapChecker, Dict[Tuple[str, str], Any]]:
    """
    Check all entities in all chains against Dev.

    Returns (GapChecker instance, gap_results dict).
    """
    logger.info("Phase 4 - Dev gap analysis")
    checker = GapChecker(dev_client)

    for chain in resolved_chains:
        gap_results = checker.check_chain(chain)
        for result in gap_results:
            audit.log(
                phase="gap_check",
                status=result.status,
                object_type=result.entity_type,
                entity_set=result.entity_set,
                external_code=result.external_code,
            )

    checker.print_gap_report()
    return checker, checker.get_results()


# ── Phase 5: Dry Run ─────────────────────────────────────────────────────────

def phase5_dry_run(
    resolved_chains: List[List[Tuple[str, str, Dict]]],
    gap_results: Dict[Tuple[str, str], Any],
    prd_cache: Dict[Tuple[str, str], Any],
    audit: AuditLogger,
    output_dir: str,
    dev_base_url: str = "",
) -> str:
    """
    Print and save dry-run payloads.  No writes to Dev.

    Returns path to the saved dry_run_summary file.
    """
    logger.info("Phase 5 - DRY RUN (no writes to Dev)")

    lines: List[str] = [
        "=" * 70,
        "  DRY RUN SUMMARY - sf_object_sync",
        f"  Generated at: {datetime.now(timezone.utc).isoformat()}",
        "=" * 70,
        "",
    ]

    # Determine upload order: collect all missing entities across all chains
    missing_by_level: Dict[str, List[Tuple[str, str, Dict]]] = {}
    for chain in resolved_chains:
        for etype, code, record in chain:
            key = (etype, code)
            gap = gap_results.get(key)
            if gap and gap.status == DEV_MISSING:
                if etype not in missing_by_level:
                    missing_by_level[etype] = []
                # Deduplicate
                if not any(c == code for _, c, _ in missing_by_level[etype]):
                    missing_by_level[etype].append((etype, code, record))

    # Integrity check: if a child is missing but its parent is also missing,
    # make sure parent appears first in upload order
    integrity_ok = True
    prev_types_seen = set()
    for etype in UPLOAD_ORDER:
        if etype in missing_by_level:
            cfg = get_config(etype)
            parent_etype = cfg["parent_entity"]
            if parent_etype and parent_etype in missing_by_level:
                if parent_etype not in prev_types_seen:
                    logger.warning(
                        "Integrity issue: %s is missing but parent %s not yet in order",
                        etype, parent_etype,
                    )
                    integrity_ok = False
        prev_types_seen.add(etype)

    total_missing = sum(len(v) for v in missing_by_level.values())
    lines.append(f"  Total entities to create: {total_missing}")
    lines.append(f"  Referential integrity:    {'OK' if integrity_ok else 'WARNING - see log'}")
    lines.append("")

    # Print each missing entity's payload
    has_missing = False
    for etype in UPLOAD_ORDER:
        entities = missing_by_level.get(etype, [])
        if not entities:
            continue
        has_missing = True
        lines.append(f"── {etype} ({get_config(etype)['entity_set']}) ──")
        lines.append(f"   Count: {len(entities)}")
        lines.append("")

        for _, code, record in entities:
            # Find the chain that contains this entity to get parent codes
            parent_codes: Dict[str, str] = {}
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
                integrity_status = DRY_RUN_OK
            except Exception as exc:
                payload_str = f"ERROR: {exc}"
                integrity_status = DRY_RUN_INTEGRITY_FAIL
                integrity_ok = False

            audit.log(
                phase="dry_run",
                status=integrity_status,
                object_type=etype,
                entity_set=get_config(etype)["entity_set"],
                external_code=code,
                payload_sent=payload if integrity_status == DRY_RUN_OK else None,
                error_message="" if integrity_status == DRY_RUN_OK else payload_str,
            )

            lines.append(f"   externalCode: {code}")
            lines.append(f"   Payload:")
            for pl in payload_str.splitlines():
                lines.append(f"     {pl}")
            lines.append("")

    if not has_missing:
        lines.append("  All entities already exist in Dev. Nothing to create.")
        lines.append("")

    lines.append("=" * 70)
    lines.append("  Review the payloads above, then re-run with dry_run: false")
    lines.append("  (and answer Yes at the prompt) to apply changes to Dev.")
    lines.append("=" * 70)

    summary_text = "\n".join(lines)
    print("\n" + summary_text)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    summary_path = os.path.join(output_dir, f"dry_run_summary_{ts}.txt")
    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write(summary_text)
    logger.info("Dry-run summary saved: %s", summary_path)

    return summary_path


# ── Phase 6: Actual Upload ────────────────────────────────────────────────────

def phase6_upload(
    resolved_chains: List[List[Tuple[str, str, Dict]]],
    gap_results: Dict[Tuple[str, str], Any],
    prd_cache: Dict[Tuple[str, str], Any],
    dev_client: SFClient,
    audit: AuditLogger,
) -> None:
    """Execute live uploads to Dev after user confirmation."""

    # ── Warning banner ───────────────────────────────────────────────────────
    print("\n" + Fore.RED + "!" * 70)
    print(Fore.RED + "  LIVE MODE - WRITES TO DEV WILL OCCUR")
    print(Fore.RED + "  Review the gap analysis above before proceeding.")
    print(Fore.RED + "!" * 70 + Style.RESET_ALL)

    missing_count = sum(
        1 for r in gap_results.values() if r.status == DEV_MISSING
    )
    print(f"\n  Entities to be created in Dev: {missing_count}")
    print()

    # ── Yes / No gate ────────────────────────────────────────────────────────
    try:
        answer = input("  Proceed with upload? [Yes/No]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer not in ("yes", "y"):
        print(Fore.YELLOW + "\n  Upload aborted by user." + Style.RESET_ALL)
        logger.info("Upload aborted - user declined")
        return

    print()
    logger.info("Phase 6 - Live upload commencing")

    uploader = Uploader(
        dev_client=dev_client,
        prd_records=prd_cache,
        gap_results=gap_results,
        audit=audit,
        dry_run=False,
    )
    uploader.run(resolved_chains)
    logger.info("Phase 6 complete")


# ── Phase 7: Report generation ────────────────────────────────────────────────

def phase7_report(
    audit: AuditLogger,
    run_meta: Dict[str, Any],
    resolved_chains: List[List[Tuple[str, str, Dict]]],
    gap_results: Dict[Tuple[str, str], Any],
    valid_rows: List[Dict[str, str]],
    validation_errors: List[Dict],
    output_dir: str,
) -> str:
    """Build object_detail_rows from chains + gap_results and generate Excel report."""
    logger.info("Phase 7 - Generating Excel report")

    from src.entity_config import ENTITY_CONFIG

    # Build one row per unique entity processed
    seen: set = set()
    object_detail_rows: List[Dict] = []

    for chain in resolved_chains:
        for etype, code, record in chain:
            key = (etype, code)
            if key in seen:
                continue
            seen.add(key)

            cfg = get_config(etype)
            gap = gap_results.get(key)
            dev_status = gap.status if gap else "UNKNOWN"

            # Determine upload status from audit records
            upload_records = [
                r for r in audit.all_records()
                if r.get("external_code") == code
                and r.get("object_type") == etype
                and r.get("phase") == "upload"
            ]
            upload_status = upload_records[-1]["status"] if upload_records else (
                "DRY_RUN" if run_meta.get("dry_run") else "NOT_PROCESSED"
            )
            error = upload_records[-1].get("error_message", "") if upload_records else ""

            object_detail_rows.append({
                "input_object": etype,
                "input_code": code,
                "level": cfg["level"],
                "entity_set": cfg["entity_set"],
                "external_code": code,
                "prd_status": "FOUND",
                "dev_status_before": dev_status,
                "action_taken": "CREATE" if dev_status == DEV_MISSING else "SKIP",
                "upload_status": upload_status,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    report_path = generate_report(
        output_dir=output_dir,
        run_meta=run_meta,
        audit_records=audit.all_records(),
        object_detail_rows=object_detail_rows,
        validation_errors=validation_errors,
    )
    return report_path


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sf_object_sync",
        description=(
            "Sync SAP SuccessFactors OM foundation objects "
            "from PRD to Dev via OData v2."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run (safe preview - default):
  python sf_object_sync.py --config config.yaml --input input.xlsx

  # Force dry run explicitly:
  python sf_object_sync.py --config config.yaml --input input.xlsx --dry-run

  # Live upload (dry_run must be false in config; requires Yes/No prompt):
  python sf_object_sync.py --config config.yaml --input input.xlsx --verbose
        """,
    )
    parser.add_argument(
        "--config", required=True, metavar="PATH",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--input", required=True, metavar="PATH",
        help="Path to input.xlsx (columns: Object, Code)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Force dry-run mode (overrides config setting)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="Set log level to DEBUG",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now(timezone.utc).isoformat()

    # ── Load config ──────────────────────────────────────────────────────────
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(Fore.RED + f"[ERROR] {exc}" + Style.RESET_ALL)
        return 1

    output_dir = cfg["options"]["output_dir"]
    log_level = "DEBUG" if args.verbose else cfg["options"].get("log_level", "INFO")
    log_path = setup_logging(log_level, output_dir)
    logger.info("sf_object_sync starting | log: %s", log_path)

    # CLI --dry-run overrides config
    dry_run: bool = args.dry_run or cfg["options"].get("dry_run", True)
    if dry_run:
        logger.info(
            "Mode: DRY RUN%s",
            " (forced by --dry-run flag)" if args.dry_run else " (from config)",
        )
    else:
        logger.info("Mode: LIVE UPLOAD")

    timeout = cfg["options"].get("request_timeout_sec", 30)

    # ── Shared state ─────────────────────────────────────────────────────────
    prd_cache: Dict[Tuple[str, str], Any] = {}
    validation_errors: List[Dict] = []
    resolved_chains: List[List[Tuple[str, str, Dict]]] = []
    gap_results: Dict[Tuple[str, str], Any] = {}

    run_meta: Dict[str, Any] = {
        "started_at": started_at,
        "config_file": os.path.abspath(args.config),
        "input_file": os.path.abspath(args.input),
        "dry_run": dry_run,
        "prd_url": cfg["prd"]["base_url"],
        "dev_url": cfg["dev"]["base_url"],
    }

    exit_code = 0

    with AuditLogger(output_dir, dry_run) as audit:
        run_meta["run_id"] = audit.run_id
        logger.info("Run ID: %s", audit.run_id)

        # ── Phase 1 ──────────────────────────────────────────────────────────
        valid_rows = phase1_validate_input(args.input, audit, validation_errors)
        run_meta["total_rows"] = len(valid_rows) + len(validation_errors)
        run_meta["valid_rows"] = len(valid_rows)

        # ── Build clients ─────────────────────────────────────────────────────
        prd_client = SFClient(
            cfg["prd"]["base_url"],
            cfg["prd"]["username"],
            cfg["prd"]["password"],
            timeout_sec=timeout,
        )
        dev_client = SFClient(
            cfg["dev"]["base_url"],
            cfg["dev"]["username"],
            cfg["dev"]["password"],
            timeout_sec=timeout,
        )

        try:
            # ── Phase 2 ──────────────────────────────────────────────────────
            confirmed_rows = phase2_prd_check(valid_rows, prd_client, audit, prd_cache)

            if not confirmed_rows:
                logger.error("No rows confirmed in PRD. Aborting.")
                return 1

            # ── Phase 3 ──────────────────────────────────────────────────────
            resolved_chains, _ = phase3_resolve_hierarchies(
                confirmed_rows, prd_client, audit, prd_cache
            )

            if not resolved_chains:
                logger.error("No hierarchies resolved. Aborting.")
                return 1

            # ── Phase 4 ──────────────────────────────────────────────────────
            checker, gap_results = phase4_gap_analysis(
                resolved_chains, dev_client, audit
            )

            # ── Phase 5 or 6 ─────────────────────────────────────────────────
            if dry_run:
                phase5_dry_run(
                    resolved_chains, gap_results, prd_cache, audit, output_dir,
                    dev_base_url=dev_client.base_url,
                )
            else:
                phase6_upload(
                    resolved_chains, gap_results, prd_cache, dev_client, audit
                )

        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            exit_code = 130
        except Exception as exc:
            logger.error("Unexpected error: %s", exc)
            logger.debug(traceback.format_exc())
            exit_code = 1
        finally:
            prd_client.close()
            dev_client.close()

        # ── Phase 7 ──────────────────────────────────────────────────────────
        run_meta["completed_at"] = datetime.now(timezone.utc).isoformat()
        try:
            report_path = phase7_report(
                audit=audit,
                run_meta=run_meta,
                resolved_chains=resolved_chains,
                gap_results=gap_results,
                valid_rows=valid_rows,
                validation_errors=validation_errors,
                output_dir=output_dir,
            )
            logger.info("Excel report: %s", report_path)
        except Exception as exc:
            logger.error("Report generation failed: %s", exc)
            logger.debug(traceback.format_exc())

        logger.info("Run complete. Audit log: %s", audit.path)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
