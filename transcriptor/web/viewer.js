import { $, $$, esc, fmtTs, fmtCount, fmtExact, fmtDate, plural, api, icons, md, LANGS, toast, modelOptionsHtml } from "./util.js";

const HUES = [250, 22, 152, 330, 200, 45, 285, 100, 0, 175];
const hue = (label) => HUES[(parseInt(String(label).replace(/\D/g, ""), 10) - 1 || 0) % HUES.length];
const norm = (s) => String(s).toLowerCase().replaceAll("ё", "е");
const root = () => $("#viewer");

let cur = null; // { job, doc, tab, query, follow, player, timer, nowIdx }
let seq = 0;
let models = {};
let preferredModel = null;

export function setModels(all, preferred) {
  models = all || {};
  preferredModel = preferred;
}

const hasTranscript = (job) => job.status === "done" || job.status === "summarizing";
const hours = () => (cur?.doc?.video?.duration || 0) >= 3600;

function speakerName(label) {
  const info = cur.doc.speakers?.[label];
  return info?.name || `Спикер ${String(label).replace(/\D/g, "")}`;
}

function teardown() {
  if (cur?.timer) clearInterval(cur.timer);
  try {
    cur?.player?.destroy?.();
  } catch {
    /* плеер мог не успеть создаться */
  }
}

/* ---------- вход ---------- */

export async function renderViewer(job) {
  teardown();
  const my = ++seq;
  if (!job) {
    cur = null;
    root().innerHTML = placeholder();
    return;
  }
  if (hasTranscript(job)) {
    const doc = await api(`/api/videos/${job.video_id}`).catch(() => null);
    if (my !== seq) return; // пока грузилось, открыли другое видео
    if (doc) {
      let tab = "summary";
      try {
        tab = localStorage.getItem("tab") || tab;
      } catch {
        /* нет доступа к хранилищу */
      }
      if (!doc.summary && job.status !== "summarizing") tab = "text";
      cur = { job, doc, tab, query: "", follow: false, player: null, timer: null, nowIdx: -1 };
      renderDoc();
      return;
    }
  }
  cur = { job };
  renderProgress(true);
}

export function onJobUpdate(job) {
  if (!cur || cur.job.id !== job.id) return;
  const before = cur.job.status;
  cur.job = job;
  if (!cur.doc) {
    if (hasTranscript(job)) renderViewer(job);
    else renderProgress(job.status !== before);
    return;
  }
  if (job.status === "running" || job.status === "queued") return renderViewer(job);
  if (before === "summarizing" && job.status === "done") return refreshDoc();
  if (job.status === "summarizing" && cur.tab === "summary") renderTab();
  renderNotes();
}

async function refreshDoc() {
  const doc = await api(`/api/videos/${cur.job.video_id}`).catch(() => null);
  if (!doc || !cur) return;
  cur.doc = doc;
  renderHead();
  renderNotes();
  renderSide();
  renderTab();
}

function placeholder() {
  return `<div class="placeholder">
    <h1>Расшифровка видео с YouTube</h1>
    <p>Вставьте слева ссылку на видео, плейлист или канал.</p>
    <ol>
      <li>yt-dlp скачает только звук.</li>
      <li>Silero VAD найдёт фрагменты речи.</li>
      <li>GigaAM v3 или Qwen3-ASR распознают речь на вашей видеокарте.</li>
      <li>pyannote определит, кто говорит.</li>
      <li>Gemini или Claude напишет пересказ и разобьёт видео на главы.</li>
    </ol>
    <div class="tech">
      <span class="chip">yt-dlp + Deno</span><span class="chip">Silero VAD</span><span class="chip">GigaAM v3</span>
      <span class="chip">Qwen3-ASR</span><span class="chip">pyannote community-1</span><span class="chip">Gemini / Claude</span>
    </div>
  </div>`;
}

/* ---------- прогресс ---------- */

const STEPS = [
  ["download", "Скачивание звука"],
  ["prepare", "Поиск речи"],
  ["language", "Определение языка"],
  ["asr", "Распознавание речи"],
  ["diarize", "Кто говорит"],
  ["summary", "Пересказ и главы"],
];

function progressBody(j) {
  if (j.status === "error") {
    return `<div class="note error">${icons.alert}<span>${esc(j.error)}</span></div>
      <div><button class="primary" data-act="retry">Повторить</button></div>`;
  }
  if (j.status === "canceled") {
    return `<p class="muted">Задание отменено.</p><div><button class="primary" data-act="retry">Запустить снова</button></div>`;
  }
  if (j.status === "queued") {
    return `<p class="muted">В очереди. Видео обрабатываются по одному, чтобы не делить видеокарту.</p>
      <div><button class="ghost" data-act="cancel">Убрать из очереди</button></div>`;
  }
  const o = j.options || {};
  const steps = STEPS.filter(
    ([k]) => (k !== "language" || o.language === "auto") && (k !== "diarize" || o.diarize) && (k !== "summary" || o.summarize),
  );
  const idx = steps.findIndex(([k]) => k === j.stage);
  const pct = Math.round((j.progress || 0) * 100);
  return `<div class="bar"><i style="width:${pct}%"></i></div>
    <p>${esc(j.message || "Готовлюсь…")} · ${pct}%</p>
    <ol class="steps">${steps
      .map(([, t], i) => `<li class="${i < idx ? "done" : i === idx ? "now" : ""}"><span class="dot"></span>${t}</li>`)
      .join("")}</ol>
    <div><button class="ghost" data-act="cancel">Остановить</button></div>`;
}

function renderProgress(full) {
  const j = cur.job;
  const dyn = $("#pv-dyn");
  if (!full && dyn) {
    dyn.innerHTML = progressBody(j);
    return;
  }
  const thumb = j.thumbnail || `https://i.ytimg.com/vi/${j.video_id}/mqdefault.jpg`;
  root().innerHTML = `<div class="progress-view">
    <img src="${esc(thumb)}" alt="" style="width:260px;max-width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:12px;margin:0 auto">
    <h1>${esc(j.title || j.url)}</h1>
    <div id="pv-dyn" style="display:flex;flex-direction:column;gap:12px">${progressBody(j)}</div>
  </div>`;
}

/* ---------- документ ---------- */

function renderDoc() {
  root().innerHTML = `<article class="doc">
    <header class="doc-head" id="doc-head"></header>
    <div class="notes" id="doc-notes"></div>
    <div class="doc-grid">
      <aside class="side">
        <div class="player" id="player"></div>
        <div id="side-panels" style="display:contents"></div>
      </aside>
      <section class="main">
        <nav class="tabs" id="tabs"></nav>
        <div id="tab-body"></div>
      </section>
    </div>
  </article>`;
  renderHead();
  renderNotes();
  renderSide();
  renderTab();
  mountPlayer();
}

function renderHead() {
  const { doc } = cur;
  const v = doc.video;
  const n = Object.keys(doc.speakers || {}).length;
  const meta = [
    v.channel,
    v.duration ? fmtTs(v.duration) : null,
    LANGS[doc.language] || doc.language,
    n ? `${n} ${plural(n, ["спикер", "спикера", "спикеров"])}` : null,
  ].filter(Boolean);
  const base = `/api/videos/${encodeURIComponent(v.id)}/export`;
  $("#doc-head").innerHTML = `<div>
      <h1><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title)}</a></h1>
      <div class="doc-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>
      <div class="doc-meta">${statsHtml(v)}</div>
    </div>
    <div class="doc-actions">
      <button class="ghost" data-act="copy">${icons.copy}Копировать</button>
      <a class="ghost" href="${base}.md" download>${icons.download}Markdown</a>
      <a class="ghost" href="${base}.txt" download>TXT</a>
      <a class="ghost" href="${base}.json" download>JSON</a>
    </div>`;
}

/** Дата загрузки, просмотры и лайки с YouTube; точные числа и дата замера — во всплывающей подсказке. */
function statsHtml(v) {
  const asOf = v.stats_at ? ` на ${fmtDate(v.stats_at)}` : "";
  // после «тыс.» и «млн» всегда «просмотров», поэтому окончание по точному числу — только до тысячи
  const count = (n, forms) =>
    `<span title="${esc(fmtExact(n))} ${plural(n, forms)}${asOf}">${esc(fmtCount(n))} ${n >= 1000 ? forms[2] : plural(n, forms)}</span>`;
  const parts = [];
  if (v.upload_date) parts.push(`<span>загружено ${esc(fmtDate(v.upload_date))}</span>`);
  if (v.view_count != null) parts.push(count(v.view_count, ["просмотр", "просмотра", "просмотров"]));
  if (v.like_count != null) parts.push(count(v.like_count, ["лайк", "лайка", "лайков"]));
  const hint = parts.length ? `Обновить просмотры и лайки (данные${asOf || " без даты"})` : "Загрузить дату и просмотры с YouTube";
  return `${parts.join("")}<button class="icon-btn small" data-act="refresh-meta" title="${esc(hint)}" aria-label="${esc(hint)}">${icons.retry}</button>`;
}

function renderNotes() {
  const notes = [...new Set([...(cur.job.notes || []), ...(cur.doc.notes || [])])];
  $("#doc-notes").innerHTML = notes.map((n) => `<div class="note">${icons.alert}<span>${esc(n)}</span></div>`).join("");
}

function renderSide() {
  const { doc } = cur;
  const speakers = Object.entries(doc.speakers || {});
  const total = speakers.reduce((s, [, i]) => s + (i.seconds || 0), 0) || 1;
  let html = "";
  if (speakers.length) {
    html += `<div class="panel"><h3>Спикеры</h3><div class="speakers">${speakers
      .map(
        ([label, info]) => `<div class="speaker-row" style="--h:${hue(label)}">
          <span class="dot"></span>
          <input type="text" value="${esc(info.name || "")}" placeholder="Спикер ${esc(label.replace(/\D/g, ""))}"
            data-label="${esc(label)}" aria-label="Имя: ${esc(label)}" maxlength="80">
          <span class="share">${Math.round((100 * (info.seconds || 0)) / total)}%</span>
          ${info.role ? `<div class="role">${esc(info.role)}</div>` : ""}
        </div>`,
      )
      .join("")}</div></div>`;
  }
  const chapters = doc.summary?.chapters || [];
  if (chapters.length) {
    html += `<div class="panel"><h3>Главы</h3><ol class="chapters">${chapters
      .map(
        (c, i) => `<li data-t="${c.start}" data-ci="${i}">
          <span class="ts">${fmtTs(c.start, hours())}</span>
          <div><div class="ct">${esc(c.title)}</div><div class="cs">${esc(c.summary)}</div></div>
        </li>`,
      )
      .join("")}</ol></div>`;
  }
  const links = doc.video.links || [];
  const description = (doc.video.description || "").trim();
  if (links.length || description) {
    const rel = 'target="_blank" rel="noopener noreferrer nofollow"';
    html += `<div class="panel"><h3>Ссылки из описания</h3>${
      links.length
        ? `<ul class="links">${links
            .map(
              (l) => `<li><a href="${esc(l.url)}" ${rel}>${esc(l.label || shortUrl(l.url))}</a>
                <span class="host">${esc(hostOf(l.url))}</span></li>`,
            )
            .join("")}</ul>`
        : `<p class="muted" style="margin:0;font-size:13.5px">В описании нет ссылок.</p>`
    }${description ? `<details class="desc"><summary>Всё описание</summary><div>${linkify(description, rel)}</div></details>` : ""}</div>`;
  }
  $("#side-panels").innerHTML = html;
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function shortUrl(url) {
  try {
    const u = new URL(url);
    return (hostOf(url) + (u.pathname === "/" ? "" : u.pathname)).slice(0, 60);
  } catch {
    return url.slice(0, 60);
  }
}

/** Текст описания с кликабельными ссылками; экранирование — до поиска ссылок. */
function linkify(text, rel) {
  return esc(text).replace(/https?:\/\/[^\s<>"']+/g, (match) => {
    let url = match;
    const entity = url.search(/&(quot|#39|lt|gt);/);
    if (entity >= 0) url = url.slice(0, entity);
    url = url.replace(/[.,;:!?)\]}»]+$/, "");
    return `<a href="${url}" ${rel}>${url}</a>${match.slice(url.length)}`;
  });
}

function renderTabs() {
  const text = cur.tab === "text";
  $("#tabs").innerHTML = `
    <button class="tab${text ? "" : " on"}" data-tab="summary">Пересказ</button>
    <button class="tab${text ? " on" : ""}" data-tab="text">Текст</button>
    <span class="spacer"></span>
    ${
      text
        ? `<span class="count" id="count"></span>
           <input type="search" id="q" placeholder="Поиск по тексту" value="${esc(cur.query)}" aria-label="Поиск по тексту">
           <button class="ghost${cur.follow ? " on" : ""}" data-act="follow" title="Прокручивать текст вслед за видео">${icons.follow}Следить</button>`
        : ""
    }`;
}

function renderTab() {
  renderTabs();
  if (cur.tab === "text") renderText();
  else renderSummary();
}

function highlight(text, q) {
  if (!q) return esc(text);
  const n = norm(text);
  let out = "";
  let i = 0;
  let j;
  while ((j = n.indexOf(q, i)) !== -1) {
    out += `${esc(text.slice(i, j))}<mark>${esc(text.slice(j, j + q.length))}</mark>`;
    i = j + q.length;
  }
  return out + esc(text.slice(i));
}

function renderText() {
  const q = norm(cur.query.trim());
  const H = hours();
  const rows = [];
  let prev = null;
  cur.doc.segments.forEach((s, i) => {
    if (q && !norm(s.text).includes(q)) return;
    const who = s.speaker && (q || s.speaker !== prev)
      ? `<span class="who" style="--h:${hue(s.speaker)}">${esc(speakerName(s.speaker))}</span><br>`
      : "";
    prev = s.speaker;
    rows.push(`<div class="seg-row" data-i="${i}">
      <button class="ts" data-t="${s.start}" title="Перейти к моменту">${fmtTs(s.start, H)}</button>
      <div>${who}<span class="text">${highlight(s.text, q)}</span></div>
    </div>`);
  });
  $("#tab-body").innerHTML = rows.length
    ? `<div class="transcript">${rows.join("")}</div>`
    : `<p class="muted">${q ? "Ничего не найдено." : "В видео не найдено речи."}</p>`;
  const count = $("#count");
  if (count) count.textContent = q ? `${rows.length} ${plural(rows.length, ["реплика", "реплики", "реплик"])}` : "";
  cur.nowIdx = -1;
}

function redo(primary) {
  return `<span style="display:inline-flex;gap:6px;align-items:center">
    <select id="redo-model" aria-label="Модель для пересказа" style="width:auto;padding:5px 8px;font-size:13px">${modelOptionsHtml(models, preferredModel)}</select>
    <button class="${primary ? "primary" : "ghost"}" data-act="summarize">${primary ? "Сделать пересказ" : "Пересказать заново"}</button>
  </span>`;
}

function renderSummary() {
  const { doc, job } = cur;
  const s = doc.summary;
  const body = $("#tab-body");
  if (job.status === "summarizing") {
    body.innerHTML = `<div class="summary-empty">
      <div class="bar indeterminate" style="width:260px"><i></i></div>
      <p>${esc(job.message || "Claude пишет пересказ…")}</p></div>`;
    return;
  }
  if (!s) {
    body.innerHTML = `<div class="summary-empty"><p>${esc(doc.summary_error || "Пересказ ещё не сделан.")}</p>${redo(true)}</div>`;
    return;
  }
  const title = models[s.model]?.title || s.model;
  const tokens = (s.usage?.input_tokens || 0) + (s.usage?.output_tokens || 0);
  const cost =
    s.cost_usd != null ? ` · $${s.cost_usd.toFixed(3)}` : tokens ? ` · ${Math.round(tokens / 1000)} тыс. токенов` : "";
  const time = doc.timings?.summary ? ` · ${Math.round(doc.timings.summary)} с` : "";
  body.innerHTML = `<div class="summary">
    <div class="tldr">${esc(s.tldr)}</div>
    <div class="prose">${md(s.summary)}</div>
    ${s.key_points?.length ? `<h3>Главное</h3><ul>${s.key_points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    <div class="foot"><span>${esc(title)}${cost}${time}</span>${redo(false)}</div>
  </div>`;
}

/* ---------- плеер ---------- */

let ytApi = null;
function loadYouTubeApi() {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (!ytApi) {
    ytApi = new Promise((resolve, reject) => {
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        prev?.();
        resolve(window.YT);
      };
      const script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      script.onerror = () => {
        ytApi = null;
        reject(new Error("YouTube API недоступен"));
      };
      document.head.append(script);
    });
  }
  return ytApi;
}

function playerFallback() {
  const el = $("#player");
  if (!el || !cur?.doc) return;
  const v = cur.doc.video;
  cur.player = null;
  el.innerHTML = `<a class="player-fallback" href="${esc(v.url)}" target="_blank" rel="noopener"
    style="background-image:linear-gradient(rgb(0 0 0/45%),rgb(0 0 0/45%)),url('${esc(v.thumbnail || "")}')">Открыть на YouTube</a>`;
}

async function mountPlayer() {
  const mine = cur;
  $("#player").innerHTML = `<div id="yt-player"></div>`;
  try {
    const YT = await loadYouTubeApi();
    if (cur !== mine) return;
    cur.player = new YT.Player("yt-player", {
      videoId: cur.doc.video.id,
      playerVars: { rel: 0, playsinline: 1, modestbranding: 1 },
      events: { onError: playerFallback },
    });
    cur.timer = setInterval(tick, 400);
  } catch {
    if (cur === mine) playerFallback();
  }
}

function seek(t) {
  const p = cur?.player;
  if (p?.seekTo) {
    p.seekTo(t, true);
    p.playVideo?.();
  } else if (cur?.doc) {
    const url = cur.doc.video.url;
    window.open(`${url}${url.includes("?") ? "&" : "?"}t=${Math.floor(t)}s`, "_blank", "noopener");
  }
}

function tick() {
  const p = cur?.player;
  if (!p?.getCurrentTime || !cur.doc) return;
  const state = p.getPlayerState?.();
  if (state !== 1 && state !== 2) return; // играет или на паузе
  const t = p.getCurrentTime() + 0.15;
  const segs = cur.doc.segments;
  let lo = 0;
  let hi = segs.length - 1;
  let idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].start <= t) {
      idx = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  if (idx !== cur.nowIdx) {
    cur.nowIdx = idx;
    $(".seg-row.now")?.classList.remove("now");
    const row = $(`.seg-row[data-i="${idx}"]`);
    row?.classList.add("now");
    if (row && cur.follow && state === 1) row.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  const chapters = cur.doc.summary?.chapters || [];
  let ci = -1;
  chapters.forEach((c, i) => {
    if (c.start <= t) ci = i;
  });
  $$(".chapters li").forEach((li) => li.classList.toggle("current", +li.dataset.ci === ci));
}

/* ---------- действия ---------- */

function plainText() {
  const H = hours();
  return cur.doc.segments
    .map((s) => `[${fmtTs(s.start, H)}]${s.speaker ? ` ${speakerName(s.speaker)}:` : ""} ${s.text}`)
    .join("\n\n");
}

async function act(name) {
  const id = cur.job.id;
  try {
    if (name === "copy") {
      await navigator.clipboard.writeText(plainText());
      toast("Текст скопирован");
    } else if (name === "refresh-meta") {
      const btn = $("[data-act=refresh-meta]");
      if (btn) btn.disabled = true;
      cur.doc.video = await api(`/api/videos/${encodeURIComponent(cur.doc.video.id)}/refresh`, { method: "POST" });
      renderHead();
      toast("Данные с YouTube обновлены");
    } else if (name === "follow") {
      cur.follow = !cur.follow;
      $("[data-act=follow]")?.classList.toggle("on", cur.follow);
    } else if (name === "summarize") {
      const model = $("#redo-model")?.value;
      await api(`/api/jobs/${id}/summarize`, { method: "POST", body: { model } });
    } else if (name === "retry" || name === "cancel") {
      await api(`/api/jobs/${id}/${name}`, { method: "POST" });
    }
  } catch (err) {
    toast(err.message);
  }
}

let searchTimer = null;
root().addEventListener("input", (e) => {
  if (e.target.id !== "q" || !cur?.doc) return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    cur.query = e.target.value;
    renderText();
  }, 120);
});

root().addEventListener("change", async (e) => {
  const label = e.target.dataset?.label;
  if (!label || !cur?.doc) return;
  try {
    cur.doc.speakers = await api(`/api/videos/${cur.doc.video.id}/speakers`, {
      method: "PATCH",
      body: { names: { [label]: e.target.value } },
    });
    if (cur.tab === "text") renderText();
    toast("Имя сохранено");
  } catch (err) {
    toast(err.message);
  }
});

root().addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.dataset?.label) e.target.blur();
});

root().addEventListener("click", (e) => {
  if (!cur) return;
  const seekEl = e.target.closest("[data-t]");
  if (seekEl) return seek(parseFloat(seekEl.dataset.t));
  const tab = e.target.closest("[data-tab]")?.dataset.tab;
  if (tab && cur.doc && tab !== cur.tab) {
    cur.tab = tab;
    try {
      localStorage.setItem("tab", tab);
    } catch {
      /* нет доступа к хранилищу */
    }
    return renderTab();
  }
  const name = e.target.closest("[data-act]")?.dataset.act;
  if (name) act(name);
});
