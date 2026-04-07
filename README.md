# SAPSF_ObjectSync

Production-grade tool to synchronise SAP SuccessFactors Organisational Management (OM) foundation objects from a **PRD** tenant to a **Dev** (lower) tenant via **OData v2 APIs**.

Available as a **CLI**, **Python library**, and **Flask web UI**.

## Supported Objects

| Level | Canonical Name  | OData Entity Set     |
|-------|-----------------|----------------------|
| 5     | Sub Department  | `cust_SubDepartment` |
| 4     | Department      | `FODepartment`       |
| 3     | Division        | `FODivision`         |
| 2     | Business Unit   | `FOBusinessUnit`     |
| 1     | Legal Entity    | `FOCompany`          |

Input rows specify **Sub Department** or **Department** only. The tool automatically resolves and syncs every ancestor up to Legal Entity.

---

## Quick Start

```bash
# 1. Clone and enter the directory
cd sf_object_sync

# 2. Install core dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
# Edit .env and fill in SF_SOURCE_URL, SF_SOURCE_USER, SF_SOURCE_PASSWORD,
#                          SF_TARGET_URL,  SF_TARGET_USER,  SF_TARGET_PASSWORD

# 4. Download the sample input template
#    (or run: python sample_data/generate_template.py)
#    Open sample_data/foundation_objects_template.xlsx and fill in your codes.

# 5a. Run via CLI (dry run — safe preview, no writes)
python -m src.sync_engine --file sample_data/foundation_objects_template.xlsx --dry-run

# 5b. OR launch the web UI
pip install -r requirements-web.txt
python web_ui/app.py
# Then open http://127.0.0.1:5000

# 6. Check output/sync_*.log for the run log
#    Check output/sync_report_*.xlsx for the full Excel report
```

---

## Authentication Methods

Set `AUTH_METHOD` in `.env` (default: `basic`):

### Basic Auth (default)
```env
AUTH_METHOD=basic
SF_SOURCE_USER=api_user@company.com
SF_SOURCE_PASSWORD=your_password
SF_TARGET_USER=api_user@company.com
SF_TARGET_PASSWORD=your_password
```

### OAuth 2.0 (Client Credentials)
```env
AUTH_METHOD=oauth
SF_SOURCE_CLIENT_ID=your_client_id
SF_SOURCE_CLIENT_SECRET=your_client_secret
SF_SOURCE_TOKEN_URL=https://<prd-host>/oauth/token
SF_TARGET_CLIENT_ID=your_client_id
SF_TARGET_CLIENT_SECRET=your_client_secret
SF_TARGET_TOKEN_URL=https://<dev-host>/oauth/token
```

### Certificate (Mutual TLS)
```env
AUTH_METHOD=certificate
SF_SOURCE_CERT_PATH=credentials/source_cert.pem
SF_SOURCE_KEY_PATH=credentials/source_key.pem
SF_TARGET_CERT_PATH=credentials/target_cert.pem
SF_TARGET_KEY_PATH=credentials/target_key.pem
```

---

## .env Setup

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `SF_SOURCE_URL` | Yes | PRD OData v2 base URL (`https://<host>/odata/v2`) |
| `SF_SOURCE_USER` | Basic only | PRD API username |
| `SF_SOURCE_PASSWORD` | Basic only | PRD API password |
| `SF_TARGET_URL` | Yes | Dev OData v2 base URL |
| `SF_TARGET_USER` | Basic only | Dev API username |
| `SF_TARGET_PASSWORD` | Basic only | Dev API password |
| `AUTH_METHOD` | No | `basic` (default) \| `oauth` \| `certificate` |
| `DRY_RUN` | No | `true` (default) \| `false` |
| `LOG_LEVEL` | No | `INFO` (default) \| `DEBUG` |
| `OUTPUT_DIR` | No | `./output` (default) |

> **Security:** Never commit `.env` to version control. It is listed in `.gitignore`.

---

## Installation

### Core (CLI only)
```bash
pip install -r requirements.txt
```

### With Web UI
```bash
pip install -r requirements.txt -r requirements-web.txt
```

---

## CLI Usage

```bash
# Dry run (env-based credentials):
python -m src.sync_engine --file input.xlsx --dry-run

# Live upload (set DRY_RUN=false in .env or omit --dry-run flag with DRY_RUN=false):
python -m src.sync_engine --file input.xlsx

# Verbose (DEBUG logging):
python -m src.sync_engine --file input.xlsx --dry-run --verbose

# Custom output dir:
python -m src.sync_engine --file input.xlsx --output-dir /tmp/sf_sync_output
```

---

## Web UI

```bash
pip install -r requirements.txt -r requirements-web.txt
python web_ui/app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

The UI provides:
- File upload (`.xlsx` / `.csv`)
- Auth method dropdown (Basic / OAuth / Certificate)
- Source + Target environment URL fields (falls back to `.env` if left blank)
- Dry-run checkbox
- Real-time progress bar
- Results page with colour-coded log and Excel report download

---

## Sample File Usage

```bash
# Generate the template:
python sample_data/generate_template.py

# Download via web UI:
# http://127.0.0.1:5000/download_template
```

See [sample_data/README.md](sample_data/README.md) for column reference and rules.

---

## Python API

```python
from src.sync_engine import sync_objects

result = sync_objects(
    source_config={
        "base_url": "https://prd.example.com/odata/v2",
        "username": "api_user@company.com",
        "password": "secret",
    },
    target_config={
        "base_url": "https://dev.example.com/odata/v2",
        "username": "api_user@company.com",
        "password": "secret",
    },
    input_file_path="input.xlsx",
    dry_run=True,
    output_dir="./output",
    progress_callback=lambda phase, msg, pct: print(f"[{pct}%] {phase}: {msg}"),
)

print(result["success"])    # entities created (or would create in dry run)
print(result["skipped"])    # already existed in target
print(result["failure"])    # failed uploads
print(result["report_path"])  # path to Excel report
```

---

## Project Structure

```
sf_object_sync/
├── .env.example               # Credential template — copy to .env
├── .gitignore                 # Excludes .env, config.yaml, mock_responses/
├── README.md
├── requirements.txt           # Core dependencies
├── requirements-web.txt       # Additional deps for web UI
├── sample_data/
│   ├── foundation_objects_template.xlsx
│   ├── generate_template.py
│   └── README.md
├── src/
│   ├── __init__.py
│   ├── sync_engine.py         # Programmatic API + CLI entry point
│   ├── auth_handler.py        # Basic / OAuth / Certificate auth
│   ├── entity_config.py       # ENTITY_CONFIG dict + constants
│   ├── sf_client.py           # OData v2 HTTP client (retry, pagination)
│   ├── hierarchy_resolver.py  # PRD parent chain traversal
│   ├── gap_checker.py         # Dev existence checks
│   ├── payload_builder.py     # POST payload construction per entity
│   ├── uploader.py            # Live upload orchestrator
│   ├── audit_logger.py        # JSONL event writer
│   └── report_generator.py   # 4-sheet Excel report
├── web_ui/
│   ├── app.py                 # Flask application
│   ├── templates/
│   │   ├── index.html         # Upload + config form
│   │   ├── status.html        # Progress polling page
│   │   └── results.html       # Sync results + log viewer
│   └── static/
│       └── style.css
├── output/                    # Runtime artefacts (git-ignored)
└── tests/
    ├── mock_responses/        # Sample OData JSON per entity (git-ignored)
    ├── test_sync_engine.py    # sync_engine unit tests
    ├── test_validator.py
    ├── test_hierarchy_resolver.py
    ├── test_gap_checker.py
    └── test_payload_builder.py
```

> **Note:** `config.yaml` and `tests/mock_responses/` are git-ignored for security. See `.gitignore` for full exclusion list.

---

## Output Files

All files are written to `OUTPUT_DIR` (default: `./output/`).

| File | Description |
|------|-------------|
| `sync_<ts>.log` | Full run log (plain text) |
| `audit_<ts>.jsonl` | One JSON line per event — machine-readable audit trail |
| `dry_run_summary_<ts>.txt` | Human-readable dry-run payloads and integrity report |
| `sync_report_<ts>.xlsx` | 4-sheet Excel workbook (Summary, Object Detail, Payload Log, Validation Errors) |

---

## Running Tests

```bash
python -m pytest tests/ -v
```

All tests mock `SFClient` — no live API calls are made.

> **Note:** Mock response JSON files in `tests/mock_responses/` are excluded from version control. See test files for inline mock data or generate fresh mocks locally.

---

## Audit Status Codes

| Status | Phase | Meaning |
|--------|-------|---------|
| `VALIDATION_FAILED` | Phase 1 | Row rejected by input validation |
| `PRD_NOT_FOUND` | Phase 2/3 | Entity or ancestor not found in PRD |
| `HIERARCHY_BROKEN` | Phase 3 | Parent field is null/empty in PRD |
| `DEV_EXISTS` | Phase 4 | Entity already exists in Dev |
| `DEV_MISSING` | Phase 4 | Entity not found in Dev — will be created |
| `DRY_RUN_OK` | Phase 5 | Payload built successfully in dry run |
| `DRY_RUN_INTEGRITY_FAIL` | Phase 5 | Payload build error in dry run |
| `UPLOAD_SUCCESS` | Phase 6 | POST returned HTTP 201 |
| `UPLOAD_FAILED` | Phase 6 | POST returned 4xx/5xx |
| `VERIFICATION_OK` | Phase 6 | Re-query confirmed entity exists |
| `VERIFICATION_FAILED` | Phase 6 | Re-query could not confirm entity |
| `SKIPPED_DUE_TO_PARENT_FAILURE` | Phase 6 | Parent entity failed; child skipped |

---

## Key Schema Notes

| Entity | Date Field | Status Field | Parent → Child Link Field |
|--------|-----------|-------------|--------------------------|
| `cust_SubDepartment` | `effectiveStartDate` | `mdfSystemStatus` | `cust_parentDepartment` → `FODepartment` |
| `FODepartment` | `startDate` | `status` | `cust_DivisionProp` → `FODivision` (**not** `parent`) |
| `FODivision` | `startDate` | `status` | `cust_BusinessUnitProp` → `FOBusinessUnit` (**not** `parent`) |
| `FOBusinessUnit` | `startDate` | `status` | `cust_legalEntityProp` → `FOCompany` (**not** `cust_parentBusinessUnit`) |
| `FOCompany` | `startDate` | `status` | *(top of hierarchy)* |

> The `parent` field in `FODepartment` and `FODivision` is a **self-referencing** tree — it is **not** the cross-entity link. Always use `cust_DivisionProp` / `cust_BusinessUnitProp` / `cust_legalEntityProp`.

---

## Retry & Rate Limiting

The HTTP client retries automatically on HTTP `429`, `500`, `502`, `503`, `504` with exponential back-off: **1s → 2s → 4s** (3 attempts total). Adjust `REQUEST_TIMEOUT_SEC` in `.env` if your tenant is slow.

---

## Security Best Practices

1. **Never commit credentials** — `.env` and `config.yaml` are git-ignored
2. **Rotate credentials immediately** if accidentally committed to version control
3. **Use certificate auth** for production environments where possible
4. **Restrict API user permissions** to the minimum required (read on PRD, write on Dev)
5. **Review `.gitignore`** before first commit to ensure sensitive files are excluded

---

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Commit your changes (`git commit -m 'Add feature'`)
4. Push to the branch (`git push origin feature/your-feature`)
5. Open a Pull Request

Ensure all tests pass (`pytest tests/ -v`) before submitting.

---

## Support

For issues, questions, or feature requests, please open an issue on GitHub.
