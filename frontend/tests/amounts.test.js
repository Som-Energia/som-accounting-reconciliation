import assert from "node:assert/strict";
import test from "node:test";

import { netBalance } from "../src/amounts.js";

test("netBalance subtracts credits from debits exactly", () => {
  assert.equal(netBalance("150.75", "40.25"), "110.50");
});

test("netBalance returns a negative balance when credits exceed debits", () => {
  assert.equal(netBalance("40.00", "150.75"), "-110.75");
});

test("netBalance retains precision for large accounting values", () => {
  assert.equal(netBalance("9007199254740993.25", "1.10"), "9007199254740992.15");
});

test("netBalance treats missing amounts as zero", () => {
  assert.equal(netBalance(undefined, "1.00"), "-1.00");
});

test("netBalance rejects amounts with more than two decimal places", () => {
  assert.throws(() => netBalance("1.001", "0.00"), TypeError);
});
