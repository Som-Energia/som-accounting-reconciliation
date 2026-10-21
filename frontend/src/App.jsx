import { useEffect, useMemo, useState } from "react";
import { netBalance } from "./amounts.js";
import { filterJournalItems, ledgerUrl } from "./ledger.js";

const today = new Date();
const dateValue = (date) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
};
const monthStart = new Date(today.getFullYear(), today.getMonth(), 1);

function money(value) {
  const raw = String(value || "0.00");
  const negative = raw.startsWith("-");
  const [whole = "0", fraction = "00"] = raw.replace("-", "").split(".");
  const grouped = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 }).format(BigInt(whole || "0"));
  return `${negative ? "−" : ""}€${grouped}.${fraction.padEnd(2, "0").slice(0, 2)}`;
}

async function request(path, options) {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function LedgerDetails({ account, runId }) {
  const [comparison, setComparison] = useState(null);
  const [completeComparison, setCompleteComparison] = useState(null);
  const [showMatched, setShowMatched] = useState(false);
  const [filters, setFilters] = useState({ dateFrom: "", dateTo: "", journal: "", partnerId: "", text: "" });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setComparison(null);
    setCompleteComparison(null);
    setShowMatched(false);
    request(`/runs/${runId}/accounts/${encodeURIComponent(account.account_code)}/items/comparison`)
      .then((result) => { if (active) setComparison(result); })
      .catch((loadError) => { if (active) setError(loadError.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [runId, account.account_code]);

  async function toggleMatched() {
    if (showMatched) {
      setShowMatched(false);
      return;
    }
    if (completeComparison) {
      setShowMatched(true);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const result = await request(
        `/runs/${runId}/accounts/${encodeURIComponent(account.account_code)}/items/comparison?include_matched=true`
      );
      setCompleteComparison(result);
      setShowMatched(true);
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }

  if (loading && !comparison) return <p className="muted">Comparing OpenERP and Odoo movements…</p>;
  if (error && !comparison) return <p className="error-inline">{error}</p>;
  const data = showMatched ? completeComparison : comparison;
  if (!data) return null;
  const journals = [...new Set([...data.source_items, ...data.target_items].map((item) => item.journal).filter(Boolean))].sort();

  return (
    <>
      {data.matched_count > 0 && <div className="details-match-toolbar">
        <span>{showMatched ? `Showing ${data.matched_count} matching movement group(s).` : `${data.matched_count} matching movement group(s) hidden.`}</span>
        <button type="button" className="matched-toggle" disabled={loading} onClick={toggleMatched}>
          {loading ? "Loading…" : showMatched ? "Hide matching movements" : `Show matching movements (${data.matched_count})`}
        </button>
      </div>}
      {error && <p className="error-inline">{error}</p>}
      <div className="ledger-filters">
        <label>Accounting date from<input type="date" value={filters.dateFrom} onChange={(event) => setFilters({ ...filters, dateFrom: event.target.value })} /></label>
        <label>Accounting date to<input type="date" value={filters.dateTo} onChange={(event) => setFilters({ ...filters, dateTo: event.target.value })} /></label>
        <label>Journal<select value={filters.journal} onChange={(event) => setFilters({ ...filters, journal: event.target.value })}><option value="">All journals</option>{journals.map((journal) => <option key={journal}>{journal}</option>)}</select></label>
        <label>Partner ID<input inputMode="numeric" value={filters.partnerId} onChange={(event) => setFilters({ ...filters, partnerId: event.target.value })} placeholder="Exact ID" /></label>
        <label>Search<input value={filters.text} onChange={(event) => setFilters({ ...filters, text: event.target.value })} placeholder="Entry, description, origin…" /></label>
        <button type="button" className="matched-toggle" onClick={() => setFilters({ dateFrom: "", dateTo: "", journal: "", partnerId: "", text: "" })}>Clear filters</button>
      </div>
      <div className="details-grid">
        <section>
          <h3>OpenERP journal items</h3>
          <JournalItems lines={data.source_items} system="source" showMatched={showMatched} filters={filters} />
        </section>
        <section>
          <h3>Odoo journal items</h3>
          <JournalItems lines={data.target_items} system="target" showMatched={showMatched} filters={filters} />
        </section>
      </div>
    </>
  );
}

function JournalItems({ lines, system, showMatched, filters }) {
  const [visibleCount, setVisibleCount] = useState(100);
  const filteredLines = filterJournalItems(lines, filters);
  const visibleLines = filteredLines.slice(0, visibleCount);
  if (!filteredLines.length) return <p className="muted">No journal items match these filters.</p>;
  return (
    <>
    <div className="detail-table-wrap">
      <table className="detail-table">
        <thead><tr><th>Date</th><th>Journal</th><th>Partner ID</th><th>Entry</th><th>Origin</th><th>Debit</th><th>Credit</th></tr></thead>
        <tbody>{visibleLines.map((line) => (
          <tr key={`${line.system}-${line.line_id}`}>
            <td>{line.date}</td><td>{line.journal || "—"}</td><td>{line.partner_id ?? "—"}</td>
            <td><strong>{line.move_label || line.move_id}</strong><small>AML {line.line_id} · {line.description}</small></td>
            <td>{line.source_model ? `${line.source_model} #${line.source_id}` : line.is_native ? "Odoo native" : "Unidentified"}{line.is_reversal ? <small>Reversal</small> : null}{line.matched ? <small className="matched-indicator">Matches other ERP</small> : null}</td>
            <td>{money(line.debit)}</td><td>{money(line.credit)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
    {visibleLines.length < filteredLines.length && <button className="load-more" onClick={() => setVisibleCount((count) => count + 100)}>{`Load more (${visibleLines.length} of ${filteredLines.length})`}</button>}
    </>
  );
}

function IssuePanel({ issues, onReview }) {
  const [filter, setFilter] = useState("all");
  const shown = filter === "all" ? issues : issues.filter((issue) => issue.type === filter);
  const types = [...new Set(issues.map((issue) => issue.type))];
  return (
    <section className="panel issue-panel">
      <div className="section-heading">
        <div><p className="eyebrow">FOLLOW-UP</p><h2>Issues and exceptions</h2></div>
        <select value={filter} onChange={(event) => setFilter(event.target.value)} aria-label="Filter issue type">
          <option value="all">All types</option>{types.map((type) => <option key={type}>{type}</option>)}
        </select>
      </div>
      {shown.length === 0 ? <p className="muted">No issues for this run.</p> : (
        <div className="issue-list">
          {shown.map((issue) => <IssueCard key={issue.id} issue={issue} onReview={onReview} />)}
        </div>
      )}
    </section>
  );
}

function IssueCard({ issue, onReview }) {
  const [assignedTo, setAssignedTo] = useState(issue.assigned_to || "");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function save(patch) {
    setBusy(true);
    setMessage("");
    try {
      await onReview(issue.id, patch);
      setMessage("Saved");
      setComment("");
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }
  return (
    <article className="issue-card">
      <div className="issue-title-row">
        <span className={`badge badge-${issue.type}`}>{issue.type.replaceAll("_", " ")}</span>
        <span className={`state state-${issue.state || "open"}`}>{issue.state || "open"}</span>
      </div>
      <h3>{issue.title}</h3>
      {issue.account_code && <p className="muted">Account {issue.account_code}</p>}
      {issue.source_model && <p className="muted">Origin: {issue.source_model} #{issue.source_id}</p>}
      {issue.details?.operation?.payment_mode && <p className="muted">Payment mode: {issue.details.operation.payment_mode}</p>}
      {issue.details?.target_records?.length > 0 && <p className="muted">Odoo matches: {issue.details.target_records.map((item) => item.label || item.target_id).join(", ")}</p>}
      {issue.details && Object.keys(issue.details).length > 0 && <details className="issue-details"><summary>Technical detail</summary><pre>{JSON.stringify(issue.details, null, 2)}</pre></details>}
      <div className="issue-controls">
        <label>Assigned to<input value={assignedTo} onChange={(event) => setAssignedTo(event.target.value)} placeholder="Name or team" /></label>
        <label>Internal note<textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Add context for the accounting team" /></label>
        <div className="button-row">
          <button disabled={busy} onClick={() => save({ assigned_to: assignedTo })}>Assign</button>
          <button disabled={busy} onClick={() => save({ state: "reviewed", comment })}>Mark reviewed</button>
          <button disabled={busy || !comment.trim()} onClick={() => save({ comment })}>Add note</button>
          <span className="muted">{message}</span>
        </div>
      </div>
      {issue.comments?.length > 0 && <div className="comments">{issue.comments.map((item, index) => <p key={index}><strong>{item.author}</strong> · {item.body}</p>)}</div>}
    </article>
  );
}

export default function App() {
  return window.location.pathname === "/ledger" ? <LedgerPage /> : <ReconciliationDashboard />;
}

function ReconciliationDashboard() {
  const [companies, setCompanies] = useState([]);
  const [runs, setRuns] = useState([]);
  const [run, setRun] = useState(null);
  const [company, setCompany] = useState("");
  const [dateFrom, setDateFrom] = useState(dateValue(monthStart));
  const [dateTo, setDateTo] = useState(dateValue(today));
  const [stateFilter, setStateFilter] = useState("posted");
  const [scope, setScope] = useState("company");
  const [statusFilter, setStatusFilter] = useState("all");
  const [expanded, setExpanded] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function refreshRuns(selectedId) {
    const result = await request("/runs");
    setRuns(result.runs);
    if (selectedId) setRun(await request(`/runs/${selectedId}`));
  }

  useEffect(() => {
    Promise.all([request("/companies"), request("/runs")])
      .then(([companyResult, runResult]) => {
        setCompanies(companyResult.companies);
        setRuns(runResult.runs);
        if (companyResult.companies.length) setCompany(companyResult.companies[0].key);
      })
      .catch((loadError) => setError(loadError.message));
  }, []);

  async function startRun(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const result = await request("/runs", {
        method: "POST",
        body: JSON.stringify({ company, date_from: dateFrom, date_to: dateTo, state_filter: stateFilter, scope }),
      });
      setRun(result);
      setExpanded("");
      await refreshRuns(result.id);
    } catch (runError) { setError(runError.message); }
    finally { setLoading(false); }
  }

  async function updateIssue(issueId, patch) {
    await request(`/issues/${encodeURIComponent(issueId)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    if (run) {
      const updated = await request(`/runs/${run.id}/issues`);
      const latest = await request(`/runs/${run.id}`);
      latest.issues = updated.issues.map((item) => ({
        id: item.id,
        type: item.issue_type,
        account_code: item.account_code,
        source_model: item.source_model,
        source_id: item.source_id,
        title: item.title,
        details: item.details,
        state: item.state,
        assigned_to: item.assigned_to,
        comments: item.comments,
      }));
      setRun(latest);
    }
  }

  const accountRows = useMemo(() => {
    const rows = run?.accounts || [];
    return rows.filter((row) => {
      if (statusFilter === "all") return true;
      if (statusFilter === "pending") {
        return row.pending_debit !== "0.00" || row.pending_credit !== "0.00";
      }
      return statusFilter === "difference"
        ? row.status === "movement_difference" || row.status === "temporal_difference"
        : row.status === statusFilter;
    });
  }, [run, statusFilter]);

  const runOptions = runs.map((item) => ({
    id: item.id,
    label: `${item.company_name} · ${item.date_from} — ${item.date_to} · ${new Date(item.created_at).toLocaleString()}`,
  }));

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top"><span className="brand-mark">S</span><span>Som Energia<small>Accounting controls</small></span></a>
        <div className="topbar-right"><span className="live-dot" /> Read-only · EUR</div>
      </header>
      <div className="content" id="top">
        <section className="hero">
          <div><p className="eyebrow">FINANCE · ERP CONTROL</p><h1>Accounting reconciliation</h1><p className="hero-copy">Compare posted debit and credit movements between OpenERP and Odoo. Investigate differences without changing either ledger.</p></div>
          <div className="hero-mark">≋</div>
        </section>

        <section className="panel filters-panel">
          <div className="section-heading"><div><p className="eyebrow">REPORT PARAMETERS</p><h2>Run a comparison</h2></div><span className="pill">Period movement is definitive</span></div>
          <form className="filters" onSubmit={startRun}>
            <label>Company<select value={company} onChange={(event) => setCompany(event.target.value)} required>{companies.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}</select></label>
            <label>Accounting date from<input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} required /></label>
            <label>Accounting date to<input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} required /></label>
            <label>Entry state<select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}><option value="posted">Posted</option><option value="all">All (diagnostic)</option></select></label>
            <label>Perimeter<select value={scope} onChange={(event) => setScope(event.target.value)}><option value="company">Full company</option><option value="synced">Synchronized only</option></select></label>
            <button className="primary-button" disabled={loading || !company}>{loading ? "Reading ledgers…" : "Run reconciliation"}<span>→</span></button>
          </form>
          {error && <div role="alert" className="error-banner">{error}</div>}
        </section>

        {runs.length > 0 && <div className="run-history"><label>Previous snapshot<select value={run?.id || ""} onChange={async (event) => { if (event.target.value) setRun(await request(`/runs/${event.target.value}`)); }}><option value="">Select a saved run</option>{runOptions.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label></div>}

        {run && <>
          <section className="summary-grid">
            <SummaryCard label="Accounts compared" value={run.totals.account_count} caption={`${run.date_from} to ${run.date_to}`} />
            <SummaryCard label="OpenERP debit" value={money(run.totals.source_debit)} caption={run.state_filter === "posted" ? "Posted movement" : "All-state movement"} />
            <SummaryCard label="Odoo debit" value={money(run.totals.target_debit)} caption="Synced + native" />
            <SummaryCard label="Issues" value={run.totals.issue_count} caption="Pending and differences" warning={run.totals.issue_count > 0} />
          </section>
          <div className="notice"><strong>Closing balances are informational.</strong> {run.cumulative_warning}{!run.cumulative_data_available ? " Cumulative totals could not be read from one or both ERP APIs; period movement is still available." : ""}</div>
          {run.company.key === "demo" && <div className="demo-banner">Fictional demonstration data. No ERP connection was made.</div>}
          {!run.totals.mapping_complete && <div className="mapping-banner"><strong>Mapping incomplete.</strong> {run.totals.mapping_issue_count} account mapping issue(s) mean the aggregate totals are partial; resolve these before treating the company comparison as complete.</div>}
          {run.scope_warning && <div className="notice"><strong>Sync-only limitation.</strong> {run.scope_warning}</div>}

          <section className="panel account-panel">
            <div className="section-heading">
              <div><p className="eyebrow">TRIAL BALANCE · PERIOD MOVEMENT</p><h2>By Odoo account</h2></div>
              <label className="compact-filter">Show<select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All accounts</option><option value="difference">Differences</option><option value="balanced">Balanced</option><option value="pending">Pending sync</option></select></label>
            </div>
            <div className="table-wrap">
              <table className="account-table">
                <thead><tr><th>Odoo account / source accounts</th><th>OpenERP debit</th><th>OpenERP credit</th><th>OpenERP balance</th><th>Odoo debit</th><th>Odoo credit</th><th>Odoo balance</th><th>Difference</th><th>Status</th></tr></thead>
                <tbody>{accountRows.map((row) => <AccountRow key={row.account_code} row={row} runId={run.id} expanded={expanded === row.account_code} onToggle={() => setExpanded(expanded === row.account_code ? "" : row.account_code)} />)}</tbody>
              </table>
              {accountRows.length === 0 && <p className="empty-state">No accounts match this filter.</p>}
            </div>
            <p className="table-footnote">OpenERP and Odoo balances are net period movements (debit minus credit), not cumulative closing balances. Odoo debit and credit include the company-native portion shown separately in each account detail. Difference keeps the existing definitive calculation and may differ from subtracting the displayed balances when pending source movements are adjusted. {run.pending_adjustment_available === false ? "Pending operations are listed separately; their AML amounts are not offset in aggregate mode, so differences remain temporal until close." : "Pending source operations are removed from the definitive difference."} Closing balances are not comparable until opening entries are validated. Account drill-down reads current ledger details on demand.</p>
          </section>

          <IssuePanel issues={run.issues || []} onReview={updateIssue} />
        </>}
        <footer>Read-only ledger access · snapshots retained for two years · all amounts in EUR · <a className="source-link" href="https://github.com/Som-Energia/som-accounting-reconciliation">Source code</a></footer>
      </div>
    </main>
  );
}

function SummaryCard({ label, value, caption, warning }) {
  return <article className={`summary-card ${warning ? "summary-warning" : ""}`}><span>{label}</span><strong>{value}</strong><small>{caption}</small></article>;
}

function AccountRow({ row, runId, expanded, onToggle }) {
  const status = row.status || "balanced";
  return <>
    <tr className={`account-row status-row-${status}`} onClick={onToggle} tabIndex="0" onKeyDown={(event) => { if (event.key === "Enter") onToggle(); }}>
      <td><button className="account-link" onClick={(event) => { event.stopPropagation(); onToggle(); }}><span className="expand-icon">{expanded ? "−" : "+"}</span><span><strong>{row.account_code} · {row.account_name || "Account"}</strong><small>OpenERP: {(row.source_account_codes || []).join(", ") || "No mapped source movement"}</small></span></button><a className="ledger-new-tab" href={ledgerUrl(runId, row.account_code)} target="_blank" rel="noopener noreferrer" onClick={(event) => event.stopPropagation()}>Open ledger in new tab ↗</a></td>
      <td>{money(row.source_debit)}</td><td>{money(row.source_credit)}</td><td>{money(netBalance(row.source_debit, row.source_credit))}</td>
      <td>{money(row.target_debit)}<small className="split">Synced {money(row.target_synced_debit)} · Native {money(row.target_native_debit)}</small></td>
      <td>{money(row.target_credit)}<small className="split">Synced {money(row.target_synced_credit)} · Native {money(row.target_native_credit)}</small></td><td>{money(netBalance(row.target_debit, row.target_credit))}</td>
      <td className={row.definitive_debit_difference === "0.00" && row.definitive_credit_difference === "0.00" ? "amount-ok" : "amount-diff"}>{money(row.definitive_balance_difference)}<small className="split">D {money(row.definitive_debit_difference)} · C {money(row.definitive_credit_difference)}</small>{row.pending_debit !== "0.00" || row.pending_credit !== "0.00" ? <small className="split">Deferred {money(row.pending_debit)} / {money(row.pending_credit)}</small> : null}</td>
      <td><span className={`badge badge-${status}`}>{status.replaceAll("_", " ")}</span>{row.pending_debit !== "0.00" || row.pending_credit !== "0.00" ? <small><span className="badge badge-pending_sync">pending</span></small> : null}</td>
    </tr>
    {expanded && <tr className="details-row"><td colSpan="9"><LedgerDetails account={row} runId={runId} /></td></tr>}
  </>;
}

function LedgerPage() {
  const params = new URLSearchParams(window.location.search);
  const runId = params.get("run_id");
  const accountCode = params.get("account_code");
  const [run, setRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!runId || !accountCode) {
      setError("A reconciliation run and account are required.");
      setLoading(false);
      return;
    }
    request(`/runs/${encodeURIComponent(runId)}`)
      .then(setRun)
      .catch((loadError) => setError(loadError.message))
      .finally(() => setLoading(false));
  }, [runId]);

  const account = run?.accounts.find((item) => item.account_code === accountCode);
  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/"><span className="brand-mark">S</span><span>Som Energia<small>Accounting controls</small></span></a>
        <div className="topbar-right"><span className="live-dot" /> Read-only · EUR</div>
      </header>
      <div className="content">
        <section className="hero">
          <div><p className="eyebrow">LEDGER DETAIL · {run?.company.name || "ACCOUNTING"}</p><h1>Account {accountCode || ""}</h1><p className="hero-copy">{account?.account_name} · {run?.date_from} to {run?.date_to}</p></div>
        </section>
        {loading && <p className="muted">Loading reconciliation…</p>}
        {error && <div role="alert" className="error-banner">{error}</div>}
        {!loading && !error && !account && <div role="alert" className="error-banner">Account not found in this reconciliation.</div>}
        {account && <section className="panel ledger-page-panel"><LedgerDetails account={account} runId={runId} /></section>}
        <footer><a className="source-link" href="/">Back to reconciliation</a> · Read-only ledger access · all amounts in EUR</footer>
      </div>
    </main>
  );
}
