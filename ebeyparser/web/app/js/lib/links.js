// The only outside web pages the app links to (opened in a new tab, never loaded by the page).
// tests/test_spa.py allows exactly these hosts in the app's JS — add new ones there too.
export const LINKS = {
  botFather: "https://t.me/BotFather",
  telegramBot: (username) => `https://t.me/${encodeURIComponent(String(username || "").replace(/^@/, ""))}`,
  lmStudio: "https://lmstudio.ai",
  ollama: "https://ollama.com",
  ebayDevelopers: "https://developer.ebay.com/my/keys",
  ebayRegister: "https://developer.ebay.com",
  googleAppPasswords: "https://myaccount.google.com/apppasswords",
  tailscale: "https://tailscale.com/download",
  kleinanzeigen: "https://www.kleinanzeigen.de",
};
