// JSON client for the EbeyParser API (/api/v1). Every screen talks to the backend through here.
//
//   import { api, ApiError } from "../lib/api.js";
//   const deals = await api.get("/deals", { params: { verdict: "buy", limit: 20 } });
//   await api.patch("/settings", { pricing: { min_profit: 50 } });
//   try { … } catch (e) { toast.error(e.message) }   // e.message is always human Russian text
//
// Errors: the server answers {"error": {"code", "message_ru"}}; older endpoints answer
// {"detail": "..."}. Both become ApiError(status, code, message). Network failures become
// ApiError(0, "network", "Нет связи с программой…") and flip appStore.connection to "offline".
import { appStore } from "./store.js";

export const API_BASE = "/api/v1";
const TOKEN_KEY = "ebp-token";

let token = null;
try {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    token = fromUrl;
    sessionStorage.setItem(TOKEN_KEY, fromUrl);
    url.searchParams.delete("token");
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
  } else {
    token = sessionStorage.getItem(TOKEN_KEY);
  }
} catch {
  /* storage blocked: cookie auth still works */
}

export function authToken() {
  return token;
}

// ------------------------------------------------------------------ human errors
// Safety net (brief §2.1–2.2): the server sends friendly Russian texts, but if a message still
// carries a CLI command, a config file name, an exception class or a local URL, the person gets
// a calm Russian sentence with a next step, and the raw text goes under «Подробнее» (`details`).
const TECH_RE =
  /python\s+-m|config\.ya?ml|\.env\b|errno|connecterror|connecttimeout|readtimeout|timeoutexception|exception|traceback|https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])|\b[a-z]+error\b|\b[a-z]+_[a-z_]+\s*[:=]|\b[a-z]+\.[a-z]+_[a-z_]+\b|allowlist|egress|getaddrinfo|ssl:|httpx|\bnone\b|\bnull\b|undefined|\bnan\b/i;
const NET_RE = /errno|connect|timeout|timed out|unreachable|refused|getaddrinfo|name or service|allowlist|egress|proxy|ssl|network|address family|host not/i;

function serviceOf(text) {
  if (/lm ?studio|localhost:1234|llama|vllm|ollama|нейросет|модел/i.test(text)) return "ai";
  if (/telegram/i.test(text)) return "Telegram";
  if (/ebay/i.test(text)) return "eBay";
  if (/smtp|gmail|gmx|outlook|e-?mail|почт/i.test(text)) return "почтовым сервером";
  if (/kleinanzeigen/i.test(text)) return "Kleinanzeigen";
  if (/tailscale/i.test(text)) return "tailscale";
  return null;
}

/** Latin-heavy text (an English upstream error) with hardly any Russian in it. */
function mostlyLatin(text) {
  const lat = (text.match(/[a-z]/gi) || []).length;
  const cyr = (text.match(/[а-яё]/gi) || []).length;
  return lat > 12 && lat > cyr * 2;
}

/**
 * { message, details } for any error text. `message` is always safe to show; `details` holds
 * the technical original (for a collapsed «Подробнее») or "" when the text was fine.
 */
export function humanize(text, { fallback } = {}) {
  const raw = String(text ?? "").trim();
  if (!raw) return { message: fallback || "Что-то пошло не так — попробуй ещё раз", details: "" };
  // light clean-up first: "Нет связи с Telegram (ConnectError) — проверь интернет"
  const cleaned = raw
    .replace(/\s*\((?:[A-Za-z]+(?:Error|Exception|Timeout))\)/g, "")
    .replace(/:\s*[a-z][a-z0-9_.]*:\s/g, ": ")
    .replace(/\s*(?:или\s+)?(?:в|через)\s+config\.ya?ml(?:\s*\/\s*\.env)?/gi, "")
    .split(/(?<=[.!?])\s+/)
    .filter((sentence) => !/python\s+-m|`[^`]+`/.test(sentence))
    .join(" ")
    .trim();
  if (cleaned && !TECH_RE.test(cleaned) && !mostlyLatin(cleaned)) return { message: cleaned, details: cleaned === raw ? "" : raw };
  const svc = serviceOf(raw);
  let message;
  if (/python\s+-m\s+ebeyparser(?!\s+\w)/i.test(raw) || /программ\w* не запущен/i.test(raw))
    message = "Программа ещё не готова — подожди минуту и попробуй снова";
  else if (svc === "eBay" && /client_id|client_secret|config|\.env|ключ/i.test(raw)) message = "eBay не подключён — добавь ключи в «Настройки → eBay»";
  else if (svc === "ai") message = "Нейросеть не отвечает — открой LM Studio и нажми Developer → Start Server";
  else if (svc === "tailscale") message = "Tailscale не найден на этом компьютере — установи его и попробуй ещё раз";
  else if (NET_RE.test(raw)) message = svc ? `Не получилось связаться с ${svc} — проверь интернет и попробуй ещё раз` : "Нет связи — проверь интернет и попробуй ещё раз";
  else if (/config\.ya?ml|\.env\b|python\s+-m/i.test(raw)) message = "Не хватает настройки — открой «Настройки» и проверь этот раздел";
  else message = fallback || "Что-то пошло не так — попробуй ещё раз. Если повторится, загляни в «Состояние → Журнал»";
  return { message, details: raw };
}

export class ApiError extends Error {
  constructor(status, code, message, data) {
    const human = humanize(message);
    super(human.message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.data = data;
    const err = data && typeof data === "object" && data.error && typeof data.error === "object" ? data.error : null;
    // technical text for «Подробнее»: the server's own `details`, or what the sanitizer replaced
    this.details = [err && typeof err.details === "string" ? err.details : "", human.details].filter(Boolean).join("\n");
    // per-field validation messages: { "pricing.min_profit": "не меньше 0", … } (PATCH /settings, POST /setup)
    const fields = data && data.error && data.error.fields;
    this.fields = Array.isArray(fields)
      ? Object.fromEntries(fields.map((f) => [f.field, f.message_ru]))
      : fields && typeof fields === "object"
        ? fields
        : {};
  }
  /** Message for one field (accepts "min_profit" or "pricing.min_profit"). */
  field(name) {
    if (this.fields[name]) return this.fields[name];
    const hit = Object.keys(this.fields).find((k) => k.endsWith("." + name) || name.endsWith("." + k));
    return hit ? this.fields[hit] : undefined;
  }
  /** The endpoint does not exist (yet) — show a friendly placeholder instead of an error. */
  get missing() {
    if (this.status === 405) return true;
    if (this.status !== 404) return false;
    return this.code === "http_404" || this.message === "Нет такого адреса API" || this.message === "Not Found";
  }
  get network() {
    return this.status === 0;
  }
}

const STATUS_TEXT = {
  400: "Проверь введённые данные",
  401: "Нужен ключ доступа — открой ссылку с ?token=… из окна программы",
  403: "Действие запрещено",
  404: "Не найдено",
  409: "Уже выполняется — подожди немного",
  413: "Слишком много данных",
  422: "Некоторые поля заполнены неверно",
  429: "Слишком много запросов — подожди минуту",
  500: "Внутренняя ошибка программы",
  502: "Сервис не ответил",
  503: "Сервис временно недоступен",
  504: "Сервис не ответил вовремя",
};

function errorFrom(status, body) {
  if (body && typeof body === "object") {
    const err = body.error;
    if (err && typeof err === "object") {
      return new ApiError(status, err.code || `http_${status}`, err.message_ru || err.message || STATUS_TEXT[status] || "Ошибка", body);
    }
    const detail = body.detail;
    if (typeof detail === "string") return new ApiError(status, `http_${status}`, detail, body);
    if (Array.isArray(detail)) {
      // FastAPI validation errors
      const msg = detail.map((d) => d && (d.msg || d.message)).filter(Boolean).slice(0, 2).join("; ");
      const e = new ApiError(status, "validation", STATUS_TEXT[422], body);
      if (msg) e.details = msg;
      return e;
    }
  }
  return new ApiError(status, `http_${status}`, STATUS_TEXT[status] || `Ошибка ${status}`, body);
}

function buildUrl(path, params) {
  const url = new URL(path.startsWith("/api/") ? path : API_BASE + path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
      else url.searchParams.set(k, v);
    }
  }
  return url.pathname + url.search;
}

/** Low-level request. `path` is relative to /api/v1 unless it starts with /api/. */
export async function request(method, path, { params, body, signal, timeout = 30000, raw = false } = {}) {
  const headers = { Accept: "application/json" };
  if (token) headers["X-EbeyParser-Token"] = token;
  let payload;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const ctrl = new AbortController();
  const timer = timeout ? setTimeout(() => ctrl.abort("timeout"), timeout) : null;
  if (signal) signal.addEventListener("abort", () => ctrl.abort(signal.reason), { once: true });
  let res;
  try {
    res = await fetch(buildUrl(path, params), {
      method,
      headers,
      body: payload,
      credentials: "same-origin",
      signal: ctrl.signal,
    });
  } catch (e) {
    if (signal && signal.aborted) throw e; // caller cancelled: not an error to show
    const timedOut = ctrl.signal.aborted;
    if (!timedOut) markOffline();
    throw new ApiError(
      0,
      timedOut ? "timeout" : "network",
      timedOut ? "Программа не ответила вовремя — попробуй ещё раз" : "Нет связи с программой. Она запущена?",
    );
  } finally {
    if (timer) clearTimeout(timer);
  }
  markOnline(res.status);
  if (raw) return res;
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw errorFrom(res.status, data);
  return data;
}

function markOffline() {
  if (appStore.get().connection !== "offline") appStore.set({ connection: "offline" });
}

function markOnline(status) {
  const next = status === 401 ? "auth" : "ok";
  if (appStore.get().connection !== next) appStore.set({ connection: next });
}

export const api = {
  get: (path, opts) => request("GET", path, opts),
  post: (path, body, opts) => request("POST", path, { ...opts, body: body ?? {} }),
  patch: (path, body, opts) => request("PATCH", path, { ...opts, body }),
  put: (path, body, opts) => request("PUT", path, { ...opts, body }),
  del: (path, opts) => request("DELETE", path, opts),
  url: buildUrl,

  /**
   * Poll GET /jobs/{id} until it finishes. Resolves with the job's `result`,
   * rejects with ApiError on failure. `onProgress(job)` gets every intermediate state.
   */
  async waitJob(id, { onProgress, interval = 1000, timeout = 180000, signal } = {}) {
    const started = Date.now();
    for (;;) {
      if (signal && signal.aborted) throw new ApiError(0, "aborted", "Отменено");
      const job = await request("GET", `/jobs/${encodeURIComponent(id)}`, { signal });
      onProgress && onProgress(job);
      const status = job && (job.status || job.state);
      if (status === "done" || status === "succeeded" || status === "finished" || status === "ok") return job.result ?? job;
      if (status === "failed" || status === "error") {
        const err = job.error || {};
        throw new ApiError(500, err.code || "job_failed", err.message_ru || err.message || job.message_ru || "Задача не удалась", job);
      }
      if (Date.now() - started > timeout) throw new ApiError(0, "timeout", "Задача выполняется слишком долго — загляни позже");
      await new Promise((r) => setTimeout(r, interval));
    }
  },
};

/** Run `fn` and turn a missing endpoint (404) into `fallback` instead of throwing. */
export async function optional(promise, fallback = null) {
  try {
    return await promise;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return fallback;
    throw e;
  }
}
