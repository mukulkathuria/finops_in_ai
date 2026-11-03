import { describe, expect, it } from "vitest";

import { amount, parseAmount, shortId, usd } from "../format";

describe("money formatting", () => {
  it("renders micro-USD exactly", () => {
    expect(usd(7)).toBe("$0.000007");
    expect(usd(1_500_000)).toBe("$1.50");
    expect(usd(1_234_567_890)).toBe("$1,234.56789");
    expect(usd(0)).toBe("$0.00");
  });

  it("parses typed dollars into integer micro-USD without float error", () => {
    expect(parseAmount("0.1", "usd")).toBe(100_000);
    expect(parseAmount("$1,000.000001", "usd")).toBe(1_000_000_001);
    expect(parseAmount("1.0000001", "usd")).toBeNull(); // below a micro-dollar
    expect(parseAmount("-5", "usd")).toBeNull();
    expect(parseAmount("abc", "tokens")).toBeNull();
    expect(parseAmount("1000000000000001", "tokens")).toBeNull(); // above the API maximum
    expect(parseAmount("2,500", "tokens")).toBe(2500);
  });

  it("shortens ids to their random tail (UUIDv7 prefixes are timestamps)", () => {
    expect(shortId("01a1a13f-06e0-76ff-9266-f911d6abdef5")).toBe("d6abdef5");
    expect(shortId(null)).toBe("-");
  });

  it("labels non-money units", () => {
    expect(amount(2500, "tokens")).toBe("2,500 tokens");
  });
});
