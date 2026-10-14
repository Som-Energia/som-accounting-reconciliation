from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def connect(database: str) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize(database: str) -> None:
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reconciliation_run (
                id TEXT PRIMARY KEY,
                company_key TEXT NOT NULL,
                company_name TEXT NOT NULL,
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                state_filter TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at TEXT NOT NULL,
                report_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reconciliation_run_company_date
                ON reconciliation_run(company_key, date_to DESC, created_at DESC);
            CREATE TABLE IF NOT EXISTS reconciliation_issue (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES reconciliation_run(id) ON DELETE CASCADE,
                fingerprint TEXT UNIQUE,
                issue_type TEXT NOT NULL,
                account_code TEXT,
                source_model TEXT,
                source_id INTEGER,
                title TEXT NOT NULL,
                details_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open',
                assigned_to TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reconciliation_issue_run
                ON reconciliation_issue(run_id, issue_type, state);
            CREATE TABLE IF NOT EXISTS reconciliation_issue_snapshot (
                run_id TEXT NOT NULL REFERENCES reconciliation_run(id) ON DELETE CASCADE,
                issue_id TEXT NOT NULL REFERENCES reconciliation_issue(id) ON DELETE CASCADE,
                issue_type TEXT NOT NULL,
                title TEXT NOT NULL,
                details_json TEXT NOT NULL,
                PRIMARY KEY (run_id, issue_id)
            );
            CREATE TABLE IF NOT EXISTS reconciliation_comment (
                id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL REFERENCES reconciliation_issue(id) ON DELETE CASCADE,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_item (
                run_id TEXT NOT NULL REFERENCES reconciliation_run(id) ON DELETE CASCADE,
                account_code TEXT NOT NULL,
                system TEXT NOT NULL,
                line_id INTEGER NOT NULL,
                line_json TEXT NOT NULL,
                PRIMARY KEY (run_id, system, line_id)
            );
            CREATE INDEX IF NOT EXISTS reconciliation_item_account
                ON reconciliation_item(run_id, account_code, system, line_id);
            CREATE TABLE IF NOT EXISTS scheduled_execution (
                job_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 1
            );
            """
        )


def save_run(database: str, report: dict[str, Any]) -> dict[str, Any]:
    run_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    report["id"] = run_id
    report["created_at"] = created_at
    report_for_api = json.loads(json.dumps(report))
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO reconciliation_run
            (id, company_key, company_name, date_from, date_to, state_filter, scope, created_at, report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                report["company"]["key"],
                report["company"]["name"],
                report["date_from"],
                report["date_to"],
                report["state_filter"],
                report["scope"],
                created_at,
                json.dumps(report, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        for index, issue in enumerate(report.get("issues", [])):
            fingerprint = _issue_fingerprint(report["company"]["key"], issue)
            existing = connection.execute(
                "SELECT id, issue_type, state FROM reconciliation_issue WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            issue_id = existing["id"] if existing else str(uuid4())
            issue["id"] = issue_id
            details_json = json.dumps(issue.get("details", {}), ensure_ascii=False)
            if existing:
                next_state = (
                    "open"
                    if existing["issue_type"] == "temporal_difference"
                    and issue["type"] == "movement_difference"
                    else existing["state"]
                )
                connection.execute(
                    """UPDATE reconciliation_issue SET run_id = ?, issue_type = ?, account_code = ?,
                    source_model = ?, source_id = ?, title = ?, details_json = ?, state = ? WHERE id = ?""",
                    (
                        run_id, issue["type"], issue.get("account_code"), issue.get("source_model"),
                        issue.get("source_id"), issue["title"], details_json, next_state, issue_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO reconciliation_issue
                    (id, run_id, fingerprint, issue_type, account_code, source_model, source_id, title, details_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        issue_id, run_id, fingerprint, issue["type"], issue.get("account_code"),
                        issue.get("source_model"), issue.get("source_id"), issue["title"], details_json, created_at,
                    ),
                )
            connection.execute(
                """INSERT OR REPLACE INTO reconciliation_issue_snapshot
                (run_id, issue_id, issue_type, title, details_json) VALUES (?, ?, ?, ?, ?)""",
                (run_id, issue_id, issue["type"], issue["title"], details_json),
            )
        for account in report.get("accounts", []):
            for system, key in (("source", "source_lines"), ("target", "target_lines")):
                for line in account.get(key, []):
                    connection.execute(
                        """INSERT OR REPLACE INTO reconciliation_item
                        (run_id, account_code, system, line_id, line_json)
                        VALUES (?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            account["account_code"],
                            system,
                            line["line_id"],
                            json.dumps(line, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
        report_for_api["issues"] = [dict(issue) for issue in report.get("issues", [])]
        for account in report_for_api.get("accounts", []):
            account["source_lines"] = []
            account["target_lines"] = []
        connection.execute(
            "UPDATE reconciliation_run SET report_json = ? WHERE id = ?",
            (json.dumps(report_for_api, ensure_ascii=False, separators=(",", ":")), run_id),
        )
    report_for_api["issues"] = [dict(issue) for issue in report.get("issues", [])]
    return report_for_api


def list_runs(database: str, limit: int = 100) -> list[dict[str, Any]]:
    with connect(database) as connection:
        rows = connection.execute(
            """SELECT id, company_key, company_name, date_from, date_to, state_filter, scope, created_at
            FROM reconciliation_run ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_run(database: str, run_id: str) -> dict[str, Any] | None:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT report_json FROM reconciliation_run WHERE id = ?", (run_id,)
        ).fetchone()
    return json.loads(row["report_json"]) if row else None


def list_items(
    database: str,
    run_id: str,
    account_code: str,
    system: str,
    *,
    offset: int = 0,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    if system not in {"source", "target"}:
        raise ValueError("System must be source or target.")
    limit = min(max(limit, 1), 500)
    with connect(database) as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM reconciliation_item WHERE run_id = ? AND account_code = ? AND system = ?",
            (run_id, account_code, system),
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT line_json FROM reconciliation_item
            WHERE run_id = ? AND account_code = ? AND system = ?
            ORDER BY line_id LIMIT ? OFFSET ?""",
            (run_id, account_code, system, limit, offset),
        ).fetchall()
    return [json.loads(row["line_json"]) for row in rows], total


def previous_comparable_run(
    database: str,
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[tuple[str, int], dict[str, Any]]] | None:
    """Return the latest earlier snapshot with the same company/window start/scope/state."""
    with connect(database) as connection:
        run = connection.execute(
            """SELECT id, date_to FROM reconciliation_run
            WHERE company_key = ? AND date_from = ? AND date_to <= ?
                AND state_filter = ? AND scope = ?
            ORDER BY date_to DESC, created_at DESC LIMIT 1""",
            (
                report["company"]["key"],
                report["date_from"],
                report["date_to"],
                report["state_filter"],
                report["scope"],
            ),
        ).fetchone()
        if not run:
            return None
        rows = connection.execute(
            """SELECT system, line_id, line_json FROM reconciliation_item
            WHERE run_id = ?""",
            (run["id"],),
        ).fetchall()
    previous_items = {
        (row["system"], int(row["line_id"])): json.loads(row["line_json"])
        for row in rows
    }
    return dict(run), previous_items


def detect_retroactive_changes(
    database: str,
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    previous = previous_comparable_run(database, report)
    if previous is None:
        return []
    previous_run, previous_items = previous
    previous_end = previous_run["date_to"]
    current_items: dict[tuple[str, int], dict[str, Any]] = {}
    for account in report.get("accounts", []):
        for field in ("source_lines", "target_lines"):
            for line in account.get(field, []):
                if line["date"] <= previous_end:
                    current_items[(line["system"], int(line["line_id"]))] = line

    changes = []
    for key in sorted(set(previous_items) | set(current_items)):
        old = previous_items.get(key)
        new = current_items.get(key)
        if old == new:
            continue
        line = new or old
        changes.append(
            {
                "type": "retroactive_change",
                "account_code": line["account_code"],
                "source_model": line.get("source_model") or "",
                "source_id": line.get("source_id"),
                "title": f"{line['system'].title()} journal item {line['line_id']} changed after the previous snapshot.",
                "details": {
                    "system": line["system"],
                    "line_id": line["line_id"],
                    "previous": old,
                    "current": new,
                    "previous_snapshot_date_to": previous_end,
                },
            }
        )
    return changes


def list_issues(database: str, run_id: str) -> list[dict[str, Any]]:
    with connect(database) as connection:
        rows = connection.execute(
            """SELECT i.*, s.run_id AS snapshot_run_id, s.issue_type AS snapshot_issue_type,
                s.title AS snapshot_title,
                s.details_json AS snapshot_details_json, r.company_name, r.date_from, r.date_to
            FROM reconciliation_issue_snapshot s
            JOIN reconciliation_issue i ON i.id = s.issue_id
            JOIN reconciliation_run r ON r.id = s.run_id
            WHERE s.run_id = ? ORDER BY i.issue_type, i.account_code, i.created_at""",
            (run_id,),
        ).fetchall()
        issues = []
        for row in rows:
            issue = dict(row)
            issue["id"] = issue.pop("id")
            issue["run_id"] = issue.pop("snapshot_run_id")
            issue["issue_type"] = issue.pop("snapshot_issue_type")
            issue["title"] = issue.pop("snapshot_title")
            issue["details"] = json.loads(issue.pop("snapshot_details_json"))
            issue.pop("fingerprint", None)
            issue.pop("details_json", None)
            issue["comments"] = [
                dict(comment)
                for comment in connection.execute(
                    "SELECT author, body, created_at FROM reconciliation_comment WHERE issue_id = ? ORDER BY created_at",
                    (issue["id"],),
                ).fetchall()
            ]
            issues.append(issue)
    return issues


def review_issue(
    database: str,
    issue_id: str,
    *,
    state: str | None = None,
    assigned_to: str | None = None,
    comment: str | None = None,
    author: str = "accounting",
) -> bool:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT id FROM reconciliation_issue WHERE id = ?", (issue_id,)
        ).fetchone()
        if not row:
            return False
        if state is not None:
            if state not in {"open", "reviewed", "resolved"}:
                raise ValueError("Issue state must be open, reviewed, or resolved.")
            connection.execute(
                "UPDATE reconciliation_issue SET state = ? WHERE id = ?",
                (state, issue_id),
            )
        if assigned_to is not None:
            connection.execute(
                "UPDATE reconciliation_issue SET assigned_to = ? WHERE id = ?",
                (assigned_to.strip(), issue_id),
            )
        if comment and comment.strip():
            connection.execute(
                "INSERT INTO reconciliation_comment (id, issue_id, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    issue_id,
                    author.strip() or "accounting",
                    comment.strip(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    return True


def purge_expired(database: str, *, today: str) -> int:
    """Delete report snapshots older than two years; issue reviews cascade with them."""
    cutoff = f"{int(today[:4]) - 2}{today[4:]}"
    with connect(database) as connection:
        cursor = connection.execute(
            "DELETE FROM reconciliation_run WHERE date_to < ?", (cutoff,)
        )
        connection.execute(
            """DELETE FROM reconciliation_issue
            WHERE id NOT IN (SELECT issue_id FROM reconciliation_issue_snapshot)"""
        )
        return cursor.rowcount


def _issue_fingerprint(company_key: str, issue: dict[str, Any]) -> str:
    issue_type = issue["type"]
    details = issue.get("details", {})
    fingerprint_type = "period_difference" if issue_type in {"temporal_difference", "movement_difference"} else issue_type
    identity: dict[str, Any] = {
        "company": company_key,
        "type": fingerprint_type,
        "account": issue.get("account_code"),
        "source_model": issue.get("source_model"),
        "source_id": issue.get("source_id"),
    }
    if issue_type == "retroactive_change":
        identity.update({"system": details.get("system"), "line_id": details.get("line_id")})
    elif issue_type == "excluded_operation":
        identity.update({"journal": details.get("journal"), "reason": details.get("reason")})
    elif issue_type == "ambiguous_origin":
        identity["target_models"] = details.get("target_models")
    elif issue_type in {"temporal_difference", "movement_difference"}:
        identity.update({"scope": details.get("scope"), "period_month": str(details.get("date_to", ""))[:7]})
    elif not (issue.get("source_model") and issue.get("source_id") is not None) and not issue.get("account_code"):
        identity["title"] = issue.get("title")
    return json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def claim_schedule(database: str, job_key: str) -> bool:
    now = datetime.now(timezone.utc)
    started_at = now.isoformat()
    stale_before = (now - timedelta(hours=2)).isoformat()
    with connect(database) as connection:
        cursor = connection.execute(
            """INSERT INTO scheduled_execution (job_key, status, started_at, attempts)
            VALUES (?, 'running', ?, 1)
            ON CONFLICT(job_key) DO UPDATE SET
                status = 'running', started_at = excluded.started_at,
                finished_at = NULL, error = NULL, attempts = scheduled_execution.attempts + 1
            WHERE scheduled_execution.attempts < 3
                AND (scheduled_execution.status = 'failed'
                    OR (scheduled_execution.status = 'running' AND scheduled_execution.started_at < ?))""",
            (job_key, started_at, stale_before),
        )
        return cursor.rowcount == 1


def finish_schedule(database: str, job_key: str, error: str | None = None) -> None:
    with connect(database) as connection:
        connection.execute(
            """UPDATE scheduled_execution SET status = ?, finished_at = ?, error = ?
            WHERE job_key = ?""",
            (
                "failed" if error else "completed",
                datetime.now(timezone.utc).isoformat(),
                error,
                job_key,
            ),
        )
