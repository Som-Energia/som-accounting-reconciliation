export function filterJournalItems(lines, filters) {
  const search = filters.text.trim().toLowerCase();
  return lines.filter((line) => {
    if (filters.dateFrom && line.date < filters.dateFrom) return false;
    if (filters.dateTo && line.date > filters.dateTo) return false;
    if (filters.journal && line.journal !== filters.journal) return false;
    if (filters.partnerId && String(line.partner_id ?? "") !== filters.partnerId.trim()) return false;
    if (search && ![
      line.move_label,
      line.description,
      line.source_model,
      line.source_id,
      line.line_id,
    ].some((value) => String(value ?? "").toLowerCase().includes(search))) return false;
    return true;
  });
}

export function ledgerUrl(runId, accountCode) {
  const query = new URLSearchParams({ run_id: runId, account_code: accountCode });
  return `/ledger?${query.toString()}`;
}
