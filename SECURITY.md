# Security Notes

SAPSF_ObjectSync is a local Flask tool for syncing foundation objects between SAP SuccessFactors tenants. It handles source and target tenant credentials.

## Before Sharing

- Remove any uploaded files from `web_ui/uploads/`.
- Do not publish screenshots containing tenant URLs, usernames, or sync results with real data.
- Use demo data for public demos.

## Secrets

- Store credentials only in `.env` (never commit it - `.env.example` is provided as a template).
- `FLASK_SECRET_KEY` should be a strong random string.
- OAuth certificates in `credentials/` are ignored by `.gitignore` - verify they are never committed.

## Security Headers

The app includes basic security headers via Flask `after_request`:
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`

## Data Handling

- Uploaded Excel/CSV files may contain organisational data.
- Sync reports and output Excel files may list all foundation objects and their values.
- Treat all output as sensitive when generated against production tenants.
