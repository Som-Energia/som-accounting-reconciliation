import assert from "node:assert/strict";
import test from "node:test";

import { filterJournalItems, ledgerUrl } from "../src/ledger.js";

const lines = [
  { date: "2026-01-10", journal: "Sales", partner_id: 42, description: "Invoice INV/1" },
  { date: "2026-02-10", journal: "Bank", partner_id: 77, description: "Payment" },
  { date: "2026-02-12", journal: "Sales", partner_id: null, description: "Native entry" },
];

test("filters journal items by date, journal, and exact partner ID", () => {
  assert.deepEqual(
    filterJournalItems(lines, {
      dateFrom: "2026-01-01",
      dateTo: "2026-01-31",
      journal: "Sales",
      partnerId: "42",
      text: "",
    }),
    [lines[0]],
  );
});

test("search matches entry descriptions and keeps missing partner IDs filterable", () => {
  assert.deepEqual(
    filterJournalItems(lines, {
      dateFrom: "",
      dateTo: "",
      journal: "",
      partnerId: "",
      text: "native ENTRY",
    }),
    [lines[2]],
  );
  assert.deepEqual(
    filterJournalItems(lines, {
      dateFrom: "",
      dateTo: "",
      journal: "",
      partnerId: "0",
      text: "",
    }),
    [],
  );
});

test("ledger detail URL safely encodes run and account identifiers", () => {
  assert.equal(
    ledgerUrl("run/1", "430 000"),
    "/ledger?run_id=run%2F1&account_code=430+000",
  );
});
