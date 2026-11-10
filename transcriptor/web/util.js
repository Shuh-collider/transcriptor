export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

/* ---------- язык интерфейса ---------- */

const html = document.documentElement;
const saved = html.dataset.langSaved === "1";
/** en | ru. Сохранённый выбор (сервер подставляет его в <html lang>) или язык браузера. */
export const lang = saved
  ? html.lang === "ru" ? "ru" : "en"
  : (navigator.languages || [navigator.language]).some((l) => /^ru\b/i.test(l || "")) ? "ru" : "en";
html.lang = lang;
const LOCALE = lang === "ru" ? "ru-RU" : "en-US";

/** Текст на языке интерфейса: L("Done", "Готово"). */
export const L = (en, ru) => (lang === "ru" ? ru : en);

async function saveLang(value) {
  await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language: value }),
  });
}
// Без сохранённого выбора сервер говорит по-английски: сообщаем ему язык браузера,
// чтобы этапы обработки и ошибки были на том же языке, что и интерфейс.
if (!saved) saveLang(lang).catch(() => {});

export async function switchLang() {
  await saveLang(lang === "ru" ? "en" : "ru").catch(() => {});
  location.reload();
}

/** Переводит статичный HTML: русский текст лежит в data-ru, data-ru-placeholder, data-ru-title, data-ru-aria-label. */
export function translateStatic(root = document) {
  if (lang !== "ru") return;
  root.querySelectorAll("[data-ru]").forEach((el) => (el.textContent = el.dataset.ru));
  for (const attr of ["placeholder", "title", "aria-label"]) {
    const key = "ru" + attr.replace(/(^|-)(\w)/g, (_, __, c) => c.toUpperCase());
    root.querySelectorAll(`[data-ru-${attr}]`).forEach((el) => el.setAttribute(attr, el.dataset[key]));
  }
  document.title = "Транскриптор";
}

/* ---------- форматирование ---------- */

export function fmtTs(sec, hours) {
  const t = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = String(t % 60).padStart(2, "0");
  return hours || h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

const COMPACT = new Intl.NumberFormat(LOCALE, { notation: "compact", maximumFractionDigits: 1 });
const EXACT = new Intl.NumberFormat(LOCALE);

/** 82169 → «82.2K» / «82,2 тыс.», 761 → «761». */
export const fmtCount = (n) => COMPACT.format(n);
export const fmtExact = (n) => EXACT.format(n);

/** «20120317» или «2026-09-28T02:10:00» → «March 17, 2012» / «17 марта 2012 г.». */
export function fmtDate(value) {
  const d = String(value || "").replace(/\D/g, "").slice(0, 8);
  if (d.length !== 8) return "";
  return new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8)).toLocaleDateString(LOCALE, {
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

/** Длительность словами: «81 h 2 min» / «81 ч 2 мин». */
export function fmtLong(sec) {
  const m = Math.round((sec || 0) / 60);
  const h = Math.floor(m / 60);
  return h ? L(`${h} h ${m % 60} min`, `${h} ч ${m % 60} мин`) : L(`${m} min`, `${m} мин`);
}

/** Форма слова для числа: plural(5, ["video", "videos"], ["видео", "видео", "видео"]). */
export function plural(n, [enOne, enMany], [one, few, many]) {
  if (lang !== "ru") return Math.abs(n) === 1 ? enOne : enMany;
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

export function ago(ts) {
  const d = (Date.now() / 1000 - ts) | 0;
  if (d < 60) return L("just now", "только что");
  if (d < 3600) return L(`${(d / 60) | 0} min ago`, `${(d / 60) | 0} мин назад`);
  if (d < 86400) return L(`${(d / 3600) | 0} h ago`, `${(d / 3600) | 0} ч назад`);
  return new Date(ts * 1000).toLocaleDateString(LOCALE, { day: "numeric", month: "short" });
}

/** Минимальный безопасный Markdown: абзацы, **жирный**, *курсив*, `код`. */
export function md(text) {
  return String(text || "")
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => {
      let html = esc(p)
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, "$1<em>$2</em>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\n/g, "<br>");
      return `<p>${html}</p>`;
    })
    .join("");
}

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || L(`Error ${res.status}`, `Ошибка ${res.status}`));
  return data;
}

export const LANGS = { ru: L("Russian", "русский"), en: L("English", "английский") };

export const PROVIDERS = { gemini: "Gemini", anthropic: "Claude" };

/** Варианты моделей по сервисам; модели без ключа видны, но недоступны. */
export function modelOptionsHtml(models, selected) {
  if (!models[selected]?.available) {
    selected = Object.keys(models).find((id) => models[id].available) || selected;
  }
  return Object.entries(PROVIDERS)
    .map(([provider, label]) => {
      const items = Object.entries(models)
        .filter(([, m]) => m.provider === provider)
        .map(
          ([id, m]) =>
            `<option value="${esc(id)}"${m.available ? "" : " disabled"}${id === selected ? " selected" : ""}>` +
            `${esc(m.title)}${m.available ? "" : L(" — no key", " — нет ключа")}</option>`,
        )
        .join("");
      return items ? `<optgroup label="${label}">${items}</optgroup>` : "";
    })
    .join("");
}

export function toast(text) {
  document.querySelector(".toast")?.remove();
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status");
  el.textContent = text;
  document.body.append(el);
  setTimeout(() => el.remove(), 3500);
}

const svg = (path, extra = "") =>
  `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${path}</svg>`;

export const icons = {
  wave: svg('<path d="M3 12h2M7 8v8M11 5v14M15 8v8M19 10v4M21 12h0"/>'),
  stop: svg('<rect x="6" y="6" width="12" height="12" rx="2"/>'),
  retry: svg('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>'),
  trash: svg('<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>'),
  download: svg('<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>'),
  search: svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
  x: svg('<path d="M18 6 6 18M6 6l12 12"/>'),
  check: svg('<path d="M20 6 9 17l-5-5"/>'),
  alert: svg('<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'),
  sparkle: svg('<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 17v4M17 19h4"/>'),
  external: svg('<path d="M14 4h6v6M10 14 20 4M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>'),
  gpu: svg('<rect x="3" y="7" width="18" height="10" rx="2"/><circle cx="9" cy="12" r="2"/><circle cx="15" cy="12" r="2"/><path d="M7 17v3M17 17v3"/>'),
  users: svg('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M21.5 20a6.5 6.5 0 0 0-4-6"/>'),
  follow: svg('<path d="M12 5v14M5 12l7 7 7-7"/>'),
  copy: svg('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>'),
};
