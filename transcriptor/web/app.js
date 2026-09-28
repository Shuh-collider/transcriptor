import { $, $$, esc, fmtTs, fmtLong, fmtCount, plural, ago, api, icons, LANGS, toast, PROVIDERS, modelOptionsHtml } from "./util.js";
import { renderViewer, onJobUpdate, setModels } from "./viewer.js";

const state = { jobs: new Map(), status: null, selected: null, filter: "" };

const DEFAULTS = {
  language: "auto",
  diarize: true,
  num_speakers: null,
  summarize: true,
  summary_model: null,
  summary_language: "ru",
  ru_engine: "gigaam",
};

function loadOpts() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem("opts") || "{}") };
  } catch {
    return { ...DEFAULTS };
  }
}
const opts = loadOpts();
function saveOpts() {
  try {
    localStorage.setItem("opts", JSON.stringify(opts));
  } catch {
    /* приватный режим — просто не запоминаем */
  }
}

/* ---------- форма ---------- */

function bindForm() {
  $$(".seg[data-opt]").forEach((seg) => {
    const key = seg.dataset.opt;
    const sync = () => $$("button", seg).forEach((b) => b.classList.toggle("on", b.dataset.value === String(opts[key])));
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      opts[key] = b.dataset.value;
      saveOpts();
      sync();
    });
    sync();
  });
  $$("input[type=checkbox][data-opt]").forEach((cb) => {
    cb.checked = !!opts[cb.dataset.opt];
    cb.addEventListener("change", () => {
      opts[cb.dataset.opt] = cb.checked;
      saveOpts();
      syncDependent();
    });
  });
  const num = $("input[data-opt=num_speakers]");
  num.value = opts.num_speakers || "";
  num.addEventListener("change", () => {
    const v = parseInt(num.value, 10);
    opts.num_speakers = v >= 1 && v <= 20 ? v : null;
    num.value = opts.num_speakers || "";
    saveOpts();
  });
  $("#model-select").addEventListener("change", (e) => {
    opts.summary_model = e.target.value;
    saveOpts();
    if (state.status) setModels(state.status.summary_models, opts.summary_model);
  });
  $("#new-form").addEventListener("submit", onSubmit);
  $("#urls").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#new-form").requestSubmit();
  });
  syncDependent();
}

function syncDependent() {
  $("#summary-opts").hidden = !opts.summarize;
  $("input[data-opt=num_speakers]").hidden = !opts.diarize;
}

function fillModels() {
  const s = state.status;
  if (!s) return;
  if (!s.summary_models[opts.summary_model]?.available) opts.summary_model = s.default_summary_model;
  $("#model-select").innerHTML = modelOptionsHtml(s.summary_models, opts.summary_model);
  setModels(s.summary_models, opts.summary_model);
}

function showError(text) {
  const el = $("#form-error");
  el.hidden = !text;
  el.textContent = text || "";
}

async function onSubmit(e) {
  e.preventDefault();
  const urls = $("#urls").value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
  if (!urls.length) return;
  const btn = $("#submit");
  btn.disabled = true;
  showError(null);
  let added = 0;
  const failed = [];
  try {
    for (const url of urls) {
      btn.textContent = urls.length > 1 ? `Смотрю ссылки… ${urls.indexOf(url) + 1}/${urls.length}` : "Смотрю ссылку…";
      let res;
      try {
        res = await api("/api/expand", { method: "POST", body: { url } });
      } catch (err) {
        failed.push(`${url}: ${err.message}`);
        continue;
      }
      let videos = res.entries;
      let batch = null;
      if (res.kind !== "video") {
        videos = await pickVideos(res);
        batch = res.title;
      }
      if (!videos.length) continue;
      const jobs = await api("/api/jobs", { method: "POST", body: { videos, options: opts, batch_title: batch } });
      jobs.forEach(upsertJob);
      added += jobs.length;
      if (added === jobs.length) select(jobs[0].id);
    }
    if (added) {
      $("#urls").value = failed.length ? failed.map((f) => f.split(": ")[0]).join("\n") : "";
      toast(`В очереди: ${added} ${plural(added, ["видео", "видео", "видео"])}`);
    }
    if (failed.length) showError(failed.join("\n"));
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Расшифровать";
  }
}

function pickVideos(res) {
  return new Promise((resolve) => {
    const dlg = $("#picker");
    const list = $("#picker-list");
    const n = res.entries.length;
    $("#picker-title").textContent = (res.kind === "channel" ? "Канал: " : "Плейлист: ") + res.title;
    $("#picker-sub").textContent = `${n} ${plural(n, ["видео", "видео", "видео"])}` + (n >= 300 ? " — показаны последние 300" : "");
    list.innerHTML = res.entries
      .map(
        (v, i) => `<li data-i="${i}"><label>
          <input type="checkbox" checked>
          <img loading="lazy" src="${esc(v.thumbnail)}" alt="">
          <span class="t">${esc(v.title)}</span>
          <span class="d">${v.duration ? fmtTs(v.duration) : ""}${v.view_count ? `<br>${fmtCount(v.view_count)} просм.` : ""}</span>
        </label></li>`,
      )
      .join("");
    const boxes = () => $$("input[type=checkbox]", list);
    const chosen = () => boxes().filter((b) => b.checked).map((b) => res.entries[+b.closest("li").dataset.i]);
    const update = () => {
      const sel = chosen();
      const total = sel.reduce((s, v) => s + (v.duration || 0), 0);
      $("#picker-total").textContent = `Выбрано ${sel.length} · ${fmtLong(total)}`;
      $("#picker-ok").textContent = `Добавить ${sel.length}`;
      $("#picker-ok").disabled = !sel.length;
    };
    list.onchange = update;
    $(".picker-tools").onclick = (e) => {
      const mode = e.target.closest("[data-pick]")?.dataset.pick;
      if (!mode) return;
      boxes().forEach((cb, i) => (cb.checked = mode === "all" ? true : mode === "none" ? false : i < 10));
      update();
    };
    const filter = $("#picker-filter");
    filter.value = "";
    filter.oninput = () => {
      const q = filter.value.trim().toLowerCase();
      $$("li", list).forEach((li) => (li.hidden = !!q && !res.entries[+li.dataset.i].title.toLowerCase().includes(q)));
    };
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? chosen() : []);
    dlg.returnValue = "";
    update();
    dlg.showModal();
  });
}

/* ---------- очередь ---------- */

function jobState(j) {
  if (j.status === "running" || j.status === "summarizing") {
    const pct = j.status === "running" ? Math.round((j.progress || 0) * 100) : null;
    return `<div class="state">${esc(j.message || "Работаю…")}${pct != null ? ` · ${pct}%` : ""}</div>
      <div class="bar${pct == null ? " indeterminate" : ""}"><i style="width:${pct ?? 30}%"></i></div>`;
  }
  if (j.status === "queued") return `<div class="state muted">В очереди</div>`;
  if (j.status === "canceled") return `<div class="state muted">Отменено</div>`;
  if (j.status === "error") return `<div class="state error">${icons.alert}<span>${esc(j.error || "Ошибка")}</span></div>`;
  const warn = j.notes?.length;
  const parts = ["Готово", LANGS[j.language] || j.language, j.cost_usd ? `$${j.cost_usd.toFixed(2)}` : null, ago(j.finished_at || j.updated_at)];
  return `<div class="state ${warn ? "warn" : "done"}">${warn ? icons.alert : icons.check}<span>${esc(parts.filter(Boolean).join(" · "))}</span></div>`;
}

function jobHtml(j) {
  const thumb = j.thumbnail || `https://i.ytimg.com/vi/${j.video_id}/mqdefault.jpg`;
  const sub = [j.channel, j.duration ? fmtTs(j.duration) : null].filter(Boolean).join(" · ");
  const act = (name, icon, title) => `<button class="icon-btn" data-act="${name}" title="${title}" aria-label="${title}">${icon}</button>`;
  const actions = [];
  if (j.status === "queued" || j.status === "running") actions.push(act("cancel", icons.stop, "Остановить"));
  if (j.status === "error" || j.status === "canceled") actions.push(act("retry", icons.retry, "Повторить"));
  if (j.status !== "running") actions.push(act("delete", icons.trash, "Убрать из списка"));
  return `<div class="job${j.id === state.selected ? " active" : ""}" data-id="${j.id}" tabindex="0">
    <img class="thumb" loading="lazy" src="${esc(thumb)}" alt="">
    <div class="body">
      ${j.batch_title ? `<span class="batch-label">${esc(j.batch_title)}</span>` : ""}
      <div class="title">${esc(j.title || j.url)}</div>
      <div class="sub">${esc(sub)}</div>
      ${jobState(j)}
    </div>
    <div class="actions">${actions.join("")}</div>
  </div>`;
}

function visibleJobs() {
  const q = state.filter.trim().toLowerCase();
  return [...state.jobs.values()]
    .sort((a, b) => b.created_at - a.created_at)
    .filter((j) => !q || `${j.title} ${j.channel} ${j.batch_title || ""}`.toLowerCase().includes(q));
}

function renderJobs() {
  const jobs = visibleJobs();
  $("#jobs").innerHTML = jobs.length
    ? jobs.map(jobHtml).join("")
    : `<p class="empty-list">${state.jobs.size ? "Ничего не найдено" : "Здесь появятся ваши расшифровки"}</p>`;
}

function renderJob(job) {
  const el = $(`.job[data-id="${job.id}"]`);
  if (!el) return renderJobs();
  const tmp = document.createElement("div");
  tmp.innerHTML = jobHtml(job);
  el.replaceWith(tmp.firstElementChild);
}

function upsertJob(job) {
  const prev = state.jobs.get(job.id);
  state.jobs.set(job.id, job);
  if (prev) renderJob(job);
  else renderJobs();
  onJobUpdate(job, prev);
}

function removeJob(id) {
  state.jobs.delete(id);
  renderJobs();
  if (state.selected === id) select(null);
}

function select(id) {
  state.selected = id;
  $$(".job").forEach((el) => el.classList.toggle("active", el.dataset.id === id));
  history.replaceState(null, "", id ? `#${id}` : location.pathname);
  renderViewer(id ? state.jobs.get(id) : null);
}

function bindJobs() {
  $("#jobs").addEventListener("click", async (e) => {
    const card = e.target.closest(".job");
    if (!card) return;
    const id = card.dataset.id;
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return select(id);
    try {
      if (act === "delete") {
        await api(`/api/jobs/${id}`, { method: "DELETE" });
        removeJob(id);
      } else {
        upsertJob(await api(`/api/jobs/${id}/${act}`, { method: "POST" }));
      }
    } catch (err) {
      toast(err.message);
    }
  });
  $("#jobs").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.classList.contains("job")) select(e.target.dataset.id);
  });
  $("#filter").addEventListener("input", (e) => {
    state.filter = e.target.value;
    renderJobs();
  });
}

async function refreshJobs() {
  const jobs = await api("/api/jobs");
  state.jobs = new Map(jobs.map((j) => [j.id, j]));
  renderJobs();
  if (state.selected && !state.jobs.has(state.selected)) select(null);
}

/* ---------- состояние ---------- */

function summaryNames() {
  const p = state.status?.providers || {};
  return Object.entries(PROVIDERS)
    .filter(([id]) => p[id])
    .map(([, label]) => label);
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  const chips = [
    s.gpu
      ? `<span class="chip ok" title="Видеокарта">${icons.gpu}${esc(s.gpu.replace(/^NVIDIA (GeForce )?/, ""))}</span>`
      : `<span class="chip bad">${icons.gpu}Нет видеокарты</span>`,
    s.hf_token ? `<span class="chip ok">${icons.users}Спикеры</span>` : `<span class="chip warn">${icons.users}Нет токена HF</span>`,
    summaryNames().length
      ? `<span class="chip ok">${icons.sparkle}${summaryNames().join(" + ")}</span>`
      : `<span class="chip warn">${icons.sparkle}Нет ключа для пересказа</span>`,
  ];
  if (s.models_loaded?.length) chips.unshift(`<span class="chip" title="Модели в видеопамяти">${esc(s.models_loaded.length)} ${plural(s.models_loaded.length, ["модель", "модели", "моделей"])} в памяти</span>`);
  $("#status").innerHTML = chips.join("");
  renderSetup();
}

function renderSetup() {
  const s = state.status;
  const ok = `<span class="mark ok">${icons.check}</span>`;
  const no = `<span class="mark no">${icons.alert}</span>`;
  const env = `<code>${esc(s.env_file)}</code>`;
  $("#setup-body").innerHTML = `
    <div class="check">${s.gpu ? ok : no}<div><b>Видеокарта</b>
      <p>${s.gpu ? `${esc(s.gpu)} — распознавание идёт на ней.` : "CUDA недоступна: всё будет работать на процессоре, в десятки раз медленнее."}</p></div></div>
    <div class="check">${s.hf_token ? ok : no}<div><b>Кто говорит — pyannote community-1</b>
      ${s.hf_token ? "<p>Токен Hugging Face найден.</p>" : `<p>Нужен бесплатный токен Hugging Face:</p><ol>
        <li>Войдите на <a href="https://huggingface.co/join" target="_blank" rel="noopener">huggingface.co</a>.</li>
        <li>Примите условия на <a href="https://huggingface.co/pyannote/speaker-diarization-community-1" target="_blank" rel="noopener">странице модели</a>.</li>
        <li>Создайте токен с правом Read в <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener">настройках</a>.</li>
        <li>Добавьте строку <code>HF_TOKEN=hf_…</code> в файл ${env} и нажмите «Проверить снова».</li></ol>`}</div></div>
    <div class="check">${summaryNames().length ? ok : no}<div><b>Пересказ и главы — Gemini или Claude</b>
      <p>${
        s.providers.gemini
          ? "Ключ Gemini найден."
          : `Gemini (есть бесплатный тариф): создайте ключ в <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Google AI Studio</a> и добавьте строку <code>GEMINI_API_KEY=…</code> в файл ${env}.`
      }</p>
      <p>${
        s.providers.anthropic
          ? "Ключ Claude найден."
          : `Claude: создайте ключ в <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">консоли Anthropic</a> и добавьте строку <code>ANTHROPIC_API_KEY=sk-ant-…</code>.`
      }</p>
      <p>После изменения <code>.env</code> нажмите «Проверить снова».</p></div></div>
    <div class="check"><span class="mark ok">${icons.wave}</span><div><b>Модели в видеопамяти</b>
      <p>${s.models_loaded?.length ? esc(s.models_loaded.join(", ")) : "Пока не загружены — загрузятся при первой расшифровке и выгрузятся после 10 минут простоя."}</p></div></div>`;
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
    renderStatus();
    fillModels();
  } catch {
    $("#status").innerHTML = `<span class="chip bad">Сервер недоступен</span>`;
  }
}

function bindStatus() {
  $("#status").addEventListener("click", () => $("#setup").showModal());
  $("#setup-refresh").addEventListener("click", async () => {
    await refreshStatus();
    toast("Настройки перечитаны");
  });
}

/* ---------- живые обновления ---------- */

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => refreshJobs().catch(() => {});
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "job") upsertJob(ev.job);
    else if (ev.type === "deleted") removeJob(ev.id);
    else if (ev.type === "status" && state.status) {
      state.status.models_loaded = ev.models_loaded;
      renderStatus();
    }
  };
}

/* ---------- старт ---------- */

bindForm();
bindJobs();
bindStatus();
await refreshStatus();
await refreshJobs();
connect();
const fromHash = location.hash.slice(1);
select(state.jobs.has(fromHash) ? fromHash : null);
window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  if (id !== state.selected) select(state.jobs.has(id) ? id : null);
});
setInterval(() => renderJobs(), 60_000); // «5 мин назад» и т. п.
