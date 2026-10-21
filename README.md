# Accounting Reconciliation

External, read-only web application for comparing OpenERP and Odoo accounting movements.

## License

This project is licensed under the GNU Affero General Public License, version 3 or (at your option) any later version. See [LICENSE](LICENSE). The application footer links to this public source repository for network users.

The application is intentionally separate from both ERP codebases. It reads journal items through each system's XML-RPC API and stores report snapshots and accountant review notes in its own SQLite database.

## Initial scope

- Compare posted debit and credit movements by mapped Odoo account over a selected accounting-date interval.
- Include all company accounts, including `572*`; the optional synchronized-only scope is identified separately in report metadata.
- Require an explicit OpenERP `odoo_account_code` mapping. Several source accounts may map to one Odoo account.
- Restrict the mapping comparison to OpenERP's 12-digit posting accounts; shorter codes are parent/view accounts and are not compared.
- Show Odoo-synchronized and Odoo-native totals separately. Native Odoo entries remain included in company scope.
- Use `Decimal` for all calculations and persist canonical amount strings. The cumulative totals are informational until opening balances are loaded and validated.
- Keep source and target journal-item details for drill-down. Do not infer one-to-one matches between different accounting object types.
- Aggregate posted period and cumulative movements at the ERP before transferring data. Store account-level snapshots and internal issue-review metadata outside both ERPs. No connector method writes to either ERP.
- Fetch journal-item detail on demand for a selected account; aggregate-mode snapshots do not retain every AML row.

## Configuration

Copy `config/companies.example.json` to a protected runtime location and set `COMPANIES_CONFIG`. Configure secret-file environment-variable names in each company profile; inject the actual credentials as read-only runtime secret files. The example uses `OPENERP_RECONCILIATION_PASSWORD_FILE` and `ODOO_RECONCILIATION_API_KEY_FILE`.

OpenERP 5 is accessed through the repository-standard `erppeek` client. Odoo 18 uses `/xmlrpc/2/common` and `/xmlrpc/2/object`; Odoo accepts an API key in place of the password. Use dedicated read-only accounts for both systems.

The app itself does not implement user accounts. Put it behind the VPN and a reverse proxy enforcing the agreed shared password. Never expose the backend or its database volume directly.

## Local development

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
flask --app run:app run --debug
pytest
```

In a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

The UI can be exercised without ERP credentials using the demo dataset. Live connections are never attempted unless a run is explicitly requested and company configuration is present.

## Periodic checks

The backend exposes one-shot commands; schedule them from the host/Portainer scheduler so multiple Gunicorn workers cannot duplicate a job:

```cron
0 2 * * * cd /opt/som-accounting-reconciliation && docker compose run --rm backend accounting-reconciliation scheduled --kind nightly
0 4 1 * * cd /opt/som-accounting-reconciliation && docker compose run --rm backend accounting-reconciliation scheduled --kind monthly
```

The dedicated, single-replica scheduler runs at 02:00 UTC each day and 04:00 UTC on the first day of each month. Nightly runs compare the rolling three-month period through yesterday; the monthly run compares from each profile's `history_start` through yesterday. It also purges snapshots older than two years. The scheduler shares the backend node and data directory and receives only the mounted read-only ERP secrets.

The Docker Compose stack expects:

- `APP_HOSTNAME`, `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT` for HTTPS routing.
- `BACKEND_NODE_HOSTNAME` and `DATA_DIR`; the data directory must be writable by UID `10001` and reside on the pinned node's local disk (SQLite WAL is not intended for a network filesystem).
- `COMPANIES_CONFIG_FILE`, `OPENERP_PASSWORD_FILE`, `ODOO_API_KEY_FILE`, and `WEB_AUTH_FILE` pointing to protected runtime files. The `WEB_AUTH_FILE` is an Nginx `htpasswd` file; no password or credential belongs in the repository.
- `BACKEND_IMAGE` and `FRONTEND_IMAGE` for deployment image references.

## Publish images to Harbor

From a clean checkout with Docker running, log in to Harbor and run the publisher with a new image tag:

```bash
docker login harbor.somenergia.coop
scripts/publish-images.sh 0.1.1
```

The script builds both images before pushing either one. It publishes the supplied tag and `latest` for the backend and frontend in the `saldos_odoo` Harbor project. Choose a new tag for each release; the script does not create or push a Git tag, or redeploy the Portainer stack.

Set `BACKEND_IMAGE` and `FRONTEND_IMAGE` in Portainer to the versioned image references printed by the script, then update the stack. The repository paths can be overridden with `BACKEND_IMAGE_REPOSITORY` and `FRONTEND_IMAGE_REPOSITORY` if the Harbor repository names change.

Create the shared web password file interactively on a protected host, then mount it as `WEB_AUTH_FILE`:

```bash
htpasswd -B -c /secure/path/accounting-reconciliation.htpasswd accounting
```

## Limitations

- No production ERP endpoints or credentials are included.
- Sync-only classification uses configured journal/invoice/payment-mode rules and exact origins, including batch-to-payment AML links where available. It still uses line-level extraction and is not suitable for very large date ranges. Some payment-order/bank streams have no shared journal-item identity; these are surfaced as ambiguous. Full-company posted movement is the scale-optimized first-phase report.
- Aggregate-mode drill-down reads current ERP line details when opened; historical snapshots preserve balances/issues, not the full row-level ERP payload.
- Opening balance comparison is provisional until accountants validate the migrated opening entries.
- This first vertical slice is not yet a deployment-ready Portainer stack; the reverse-proxy password and secret mounts must be supplied by the deployment environment.
