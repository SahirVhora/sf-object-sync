# Sample Data

This directory contains the input template and reference data for sf_object_sync.

## Files

| File | Description |
|------|-------------|
| `foundation_objects_template.xlsx` | Sample input file - open in Excel and fill in your codes |
| `generate_template.py` | Script to regenerate the template xlsx |

---

## foundation_objects_template.xlsx - Column Reference

The input file requires **exactly two columns** (case-insensitive, extra whitespace is trimmed):

| Column | Required | Description | Valid values |
|--------|----------|-------------|--------------|
| `Object` | Yes | The OM foundation object type | `Sub Department`, `Department` |
| `Code` | Yes | The `externalCode` of the object in PRD | Alphanumeric, hyphens and underscores allowed |

### Rules

- **Header row** must contain both `Object` and `Code` (any column order, any row - the tool scans for the first row containing both).
- **Blank rows** are silently skipped.
- **Object type** matching is case-insensitive: `sub department`, `Sub Department`, `SUB DEPARTMENT` are all accepted.
- **Codes** must be alphanumeric (hyphens `-` and underscores `_` are also allowed). Leading/trailing spaces are trimmed.
- Invalid rows are rejected and written to the **Validation Errors** sheet in the Excel report - processing continues for valid rows.

### Example

| Object | Code |
|---|---|
| Sub Department | 10000073 |
| Department | 10016236 |
| sub department | 10000099 |
| Department | DEPT-001 |

---

## How the tool uses your input

1. **Validates** every row (Object type + Code format).
2. **Checks PRD** - confirms each code exists as an active record.
3. **Resolves parent chain** - for `Sub Department` it fetches: Department → Division → Business Unit → Legal Entity.
4. **Checks Dev** - identifies which ancestors are already present.
5. **Dry run** - prints POST payloads for all missing entities (no writes).
6. **Live upload** - POSTs missing entities in top-down order (Legal Entity → Sub Department).

---

## Generating the template

```bash
python sample_data/generate_template.py
```

This creates `sample_data/foundation_objects_template.xlsx` with sample rows and formatting.

---

## What you do NOT need to include

You only need to specify the **bottom-most** objects you want in Dev.
The tool automatically resolves and syncs every ancestor - you don't need to list
Division, Business Unit, or Legal Entity rows manually.
