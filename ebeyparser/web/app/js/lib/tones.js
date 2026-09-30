// Colour means money, always the same way (brief §2.6, §6.1). Single source of truth for hues.
//   green → profit / buy / brand · amber → haggle / offer · violet → auction / bid
//   red → danger / scam / loss · blue → info / AI · neutral → maybe / skip

const ALIASES = { profit: "green", brand: "green", success: "green", buy: "green", haggle: "amber", warning: "amber", bid: "violet", auction: "violet", danger: "red", error: "red", scam: "red", info: "blue", ai: "blue" };

/** Normalise a tone name ("profit", "haggle", "green" …) to a hue class suffix. */
export function hue(tone) {
  if (!tone) return "neutral";
  return ALIASES[tone] || tone;
}

/** Hue for a deal decision: action (buy/haggle/bid/watch/skip) wins over verdict (buy/maybe/skip). */
export function verdictTone({ action, verdict, purpose, redFlags } = {}) {
  if (redFlags) return "red";
  if (action === "buy") return "green";
  if (action === "haggle") return "amber";
  if (action === "bid") return "violet";
  if (action === "watch" || action === "skip") return "neutral";
  if (verdict === "buy") return purpose === "personal" ? "green" : "green";
  return "neutral";
}

/** Icon per decision (§6.9). */
export const DECISION_ICONS = {
  buy: "trending-up",
  haggle: "hand-coins",
  bid: "gavel",
  watch: "eye",
  skip: "eye-off",
  personal: "piggy-bank",
  free: "gift",
};
