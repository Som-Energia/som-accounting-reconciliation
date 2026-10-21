function toCents(value) {
  const match = /^([+-]?)(\d+)(?:\.(\d{0,2}))?$/.exec(String(value ?? "0").trim());
  if (!match) throw new TypeError(`Invalid accounting amount: ${value}`);

  const cents = BigInt(match[2]) * 100n + BigInt((match[3] || "").padEnd(2, "0") || "0");
  return match[1] === "-" ? -cents : cents;
}

export function netBalance(debit, credit) {
  const cents = toCents(debit) - toCents(credit);
  const sign = cents < 0n ? "-" : "";
  const absolute = cents < 0n ? -cents : cents;
  const whole = absolute / 100n;
  const fraction = String(absolute % 100n).padStart(2, "0");
  return `${sign}${whole}.${fraction}`;
}
