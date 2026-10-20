const $ = selector => document.querySelector(selector);
const PAGE_SIZE = 5;
const MAX_BATCH = 50;
const terminal = new Set(['done', 'error', 'cancelled']);
const labels = {queued:'排队中', extracting:'解析中', downloading:'下载中', converting:'校验中', done:'已完成', error:'失败', cancelled:'已取消'};
const batchCards = new Map();
const batchPages = new Map();
let playingJob = null;
function text(tag, value, className) {
  const node = document.createElement(tag); node.textContent = value;
  if (className) node.className = className;
  return node;
}
function button(label, action, className = 'secondary') {
  const node = text('button', label, className); node.type = 'button';
  node.addEventListener('click', action); return node;
}
function showError(error, target = '#error') {
  $(target).textContent = error.message || '请求失败，请重试。'; $(target).hidden = false;
}
async function cancel(url) {
  $('#queue-error').hidden = true;
  try { await postJson(url, {}); await refresh(); }
  catch (error) { showError(error, '#queue-error'); }
}
function play(job) {
  $('#player-panel').hidden = false;
  $('#player-title').textContent = job.title;
  if (playingJob !== job.id) { $('#player').src = job.download; playingJob = job.id; }
  $('#player').play().catch(() => {
    if (playingJob === job.id) $('#player-title').textContent = `${job.title} · 请使用播放器控件开始试听`;
  });
}
function createPager(label, change) {
  const nav = text('nav', '', 'pagination'); nav.setAttribute('aria-label', label);
  const select = document.createElement('select'); select.setAttribute('aria-label', `${label}页码`);
  const previous = button('上一页', () => change(Number(select.value) - 1));
  const next = button('下一页', () => change(Number(select.value) + 1));
  const caption = text('label', '第 '); const total = text('span', '');
  caption.append(select, total);
  select.addEventListener('change', () => change(Number(select.value)));
  nav.append(previous, caption, next); nav.controls = {select, previous, next, total};
  return nav;
}
function updatePager(nav, page, pages) {
  const {select, previous, next, total} = nav.controls;
  // Preserve the native select during polling; only rebuild options if needed.
  if (select.children.length !== pages) {
    select.replaceChildren();
    for (let value = 1; value <= pages; value++) {
      const option = text('option', String(value)); option.value = String(value); select.append(option);
    }
  }
  select.value = String(page); previous.disabled = page === 1; next.disabled = page === pages;
  total.textContent = ` 页 / 共 ${pages} 页`;
}
function renderBatch(batch, card) {
  const pages = Math.max(1, Math.ceil(batch.jobs.length / PAGE_SIZE));
  const page = Math.min(batchPages.get(batch.id) || 1, pages);
  batchPages.set(batch.id, page);
  const signature = JSON.stringify([batch, page]);
  if (card.dataset.signature === signature) return;
  card.dataset.signature = signature;
  if (!card.heading) {
    card.heading = text('div', '', 'batch-heading');
    card.summary = text('p', '', 'batch-summary'); card.rows = text('div', '', 'batch-jobs');
    card.pager = createPager(`${batch.title}任务分页`, nextPage => {
      batchPages.set(batch.id, nextPage); renderBatch(card.batch, card);
    });
    card.append(card.heading, card.summary, card.rows, card.pager);
  }
  card.batch = batch;
  const heading = card.heading; heading.replaceChildren();
  heading.append(text('h3', batch.title));
  const queued = batch.jobs.filter(job => job.status === 'queued').length;
  if (queued) heading.append(button('取消等待中的任务', () => cancel(`/api/batches/${batch.id}/cancel`)));
  const done = batch.jobs.filter(job => job.status === 'done').length;
  const failed = batch.jobs.filter(job => job.status === 'error').length;
  const cancelled = batch.jobs.filter(job => job.status === 'cancelled').length;
  const active = batch.jobs.find(job => !terminal.has(job.status) && job.status !== 'queued');
  card.summary.textContent = `${batch.created} · 共 ${batch.total} 个 · 已完成 ${done} · 失败 ${failed} · 已取消 ${cancelled} · 等待 ${queued}${active ? ` · 正在处理 P${active.page}` : ''}`;
  const rows = card.rows; rows.replaceChildren();
  for (const job of batch.jobs.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)) {
    const row = text('article', '', `job${job.status === 'error' ? ' error-state' : ''}`);
    const top = text('div', '', 'job-top');
    top.append(text('h4', `P${job.page} · ${job.title}`));
    const actions = text('div', '', 'job-actions');
    if (job.status === 'done') {
      actions.append(button('试听', () => play(job)));
      const link = text('a', '下载 M4A', 'download'); link.href = job.download + '?download=1'; actions.append(link);
    } else if (job.status === 'queued') {
      actions.append(button('取消', () => cancel(`/api/jobs/${job.id}/cancel`)));
    }
    top.append(actions); row.append(top);
    const detail = job.status === 'done'
      ? `${Math.floor(job.duration / 60)}:${String(Math.floor(job.duration % 60)).padStart(2, '0')} · ${(job.size / 1048576).toFixed(1)} MB`
      : job.message;
    row.append(text('p', `${labels[job.status]} · ${detail}`));
    if (!terminal.has(job.status)) {
      const progress = document.createElement('progress'); progress.max = 100;
      if (job.progress !== null) progress.value = job.progress;
      progress.setAttribute('aria-label', `P${job.page} 提取进度`); row.append(progress);
    }
    rows.append(row);
  }
  card.pager.hidden = pages <= 1;
  updatePager(card.pager, page, pages);
}
function render(batches) {
  $('#empty').hidden = batches.length > 0;
  const jobs = batches.flatMap(batch => batch.jobs);
  $('#count').textContent = `${batches.length} 个批次 · ${jobs.length} 个任务`;
  const unfinished = jobs.filter(job => !terminal.has(job.status)).length;
  $('#queue-status').textContent = `未完成 ${unfinished} / 100 · 串行处理 · 关闭程序将中止队列`;
  const present = new Set(batches.map(batch => batch.id));
  for (const [id, card] of batchCards) {
    if (!present.has(id)) { card.remove(); batchCards.delete(id); batchPages.delete(id); }
  }
  for (const batch of batches) {
    let card = batchCards.get(batch.id);
    if (!card) { card = text('section', '', 'batch'); batchCards.set(batch.id, card); }
    renderBatch(batch, card);
  }
  batches.forEach((batch, index) => {
    const node = batchCards.get(batch.id);
    if ($('#jobs').children[index] !== node) $('#jobs').insertBefore(node, $('#jobs').children[index] || null);
  });
}
async function refresh() {
  try {
    const response = await fetch('/api/batches'); if (!response.ok) throw Error();
    render(await response.json()); $('#connection').hidden = true;
  } catch {
    $('#connection').textContent = '暂时无法连接本地服务，请确认启动窗口仍在运行。'; $('#connection').hidden = false;
  }
}

// Metadata and selection belong to one exact input revision.
let revision = 0;
let metadata = null;
let pending = null;
let controller = null;
let debounce = null;
let expanded = false;
let submitting = false;
let selected = new Set();
let selectionPage = 1;
function pageParts() {
  return filteredParts().slice((selectionPage - 1) * PAGE_SIZE, selectionPage * PAGE_SIZE);
}
function updateSubmit() {
  const visible = metadata && expanded ? pageParts() : [];
  const local = visible.filter(part => selected.has(part.page)).length;
  const hidden = selected.size - local;
  $('#selected-count').textContent = `已选 ${selected.size} 个（本页 ${local} 个）${hidden ? `，另有 ${hidden} 个位于其他页或隐藏结果中` : ''}`;
  $('#selection-limit').hidden = selected.size <= MAX_BATCH;
  $('#selection-limit').textContent = selected.size > MAX_BATCH ? '超过单批 50 个上限，请减少选择后分批提交。' : '';
  $('#select-page').checked = visible.length > 0 && local === visible.length;
  $('#select-page').indeterminate = local > 0 && local < visible.length;
  $('#select-page').disabled = !visible.length || submitting;
  $('#submit').textContent = submitting ? '正在提交…' : expanded ? `提取所选 ${selected.size} 个分 P ↗` : '获取音频 ↗';
  $('#submit').disabled = submitting || (expanded && (!selected.size || selected.size > MAX_BATCH));
  // Freeze selection while submitting so a successful request cannot clear edits
  // made after its payload was captured. The source URL may still be replaced.
  $('#part-search').disabled = submitting;
  $('#select-all').disabled = submitting;
  $('#clear-selection').disabled = submitting;
  $('#parts-jump').disabled = submitting;
  $('#parts-prev').disabled = submitting || selectionPage === 1;
  $('#parts-next').disabled = submitting || !metadata || selectionPage >= Math.max(1, Math.ceil(filteredParts().length / PAGE_SIZE));
  for (const checkbox of $('#part-list').querySelectorAll('input')) checkbox.disabled = submitting;
}
function resetVideo() {
  revision++; clearTimeout(debounce);
  if (controller) controller.abort();
  controller = null; pending = null; metadata = null; expanded = false;
  selected = new Set(); selectionPage = 1;
  $('#part-search').value = '';
  $('#parts').hidden = true; $('#part-list').replaceChildren();
  $('#error').hidden = true;
  $('#video-status').textContent = $('#url').value.trim() ? '等待识别…' : '粘贴链接后自动识别分 P';
  updateSubmit();
}

async function postJson(url, body, signal) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-App-Token': $('meta[name="app-token"]').content},
    body: JSON.stringify(body), signal
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '请求失败，请重试。');
  return result;
}

function inspectVideo() {
  if (metadata) return Promise.resolve(metadata);
  if (pending) return pending;
  const current = revision;
  const input = $('#url').value.trim();
  if (!input) return Promise.resolve(null);
  controller = new AbortController();
  const requestController = controller;
  const timeout = setTimeout(() => requestController.abort(), 30000);
  $('#video-status').textContent = '正在识别视频和分 P…';
  pending = (async () => {
    try {
      const result = await postJson('/api/video', {url: input}, requestController.signal);
      if (current !== revision) return null;
      metadata = result;
      $('#video-status').textContent = result.parts.length > 1
        ? `已识别 ${result.parts.length} 个分 P，点击获取后选择`
        : '已识别为单 P，点击获取即可提取';
      return result;
    } catch (error) {
      if (current !== revision) return null;
      const message = error.name === 'AbortError' ? '识别超时，请点击获取重试。' : (error.message || '识别失败，请点击获取重试。');
      $('#video-status').textContent = message;
      throw Error(message);
    } finally {
      clearTimeout(timeout);
      if (current === revision) { pending = null; controller = null; }
    }
  })();
  return pending;
}

function filteredParts() {
  const query = $('#part-search').value.trim().toLocaleLowerCase();
  return metadata.parts.filter(part => !query || String(part.page).includes(query)
    || `p${part.page}`.includes(query) || part.title.toLocaleLowerCase().includes(query));
}
function renderSelection() {
  const parts = filteredParts();
  const pages = Math.max(1, Math.ceil(parts.length / PAGE_SIZE));
  selectionPage = Math.max(1, Math.min(selectionPage, pages));
  $('#part-list').replaceChildren();
  for (const part of parts.slice((selectionPage - 1) * PAGE_SIZE, selectionPage * PAGE_SIZE)) {
    const row = document.createElement('label'); row.className = 'part-option';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.value = part.page; checkbox.checked = selected.has(part.page);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selected.add(part.page); else selected.delete(part.page);
      updateSubmit();
    });
    row.append(checkbox, text('span', `P${part.page} · ${part.title}`)); $('#part-list').append(row);
  }
  if (!parts.length) $('#part-list').append(text('p', '没有匹配的分 P', 'video-status'));
  const jump = $('#parts-jump');
  if (jump.children.length !== pages) {
    jump.replaceChildren();
    for (let page = 1; page <= pages; page++) {
      const option = text('option', String(page)); option.value = String(page); jump.append(option);
    }
  }
  jump.value = String(selectionPage);
  $('#parts-page').textContent = ` 页 / 共 ${pages} 页 · 匹配 ${parts.length} 个`;
  $('#parts-prev').disabled = selectionPage === 1;
  $('#parts-next').disabled = selectionPage === pages;
  $('#select-page').disabled = !parts.length;
  $('#select-all').textContent = `选择全部 ${metadata.parts.length} 个`;
  updateSubmit();
}
function showParts(info) {
  selected = new Set([info.selected_page]);
  selectionPage = Math.floor(info.parts.findIndex(part => part.page === info.selected_page) / PAGE_SIZE) + 1;
  $('#video-title').textContent = info.title;
  expanded = true; $('#parts').hidden = false;
  $('#video-status').textContent = `共 ${info.parts.length} 个分 P；每批最多 50 个，按 P 编号依次提取。`;
  renderSelection();
}
$('#part-search').addEventListener('input', () => { selectionPage = 1; renderSelection(); });
$('#part-search').addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
$('#parts-prev').addEventListener('click', () => { selectionPage--; renderSelection(); });
$('#parts-next').addEventListener('click', () => { selectionPage++; renderSelection(); });
$('#select-page').addEventListener('change', () => {
  const checked = $('#select-page').checked;
  for (const part of pageParts()) { if (checked) selected.add(part.page); else selected.delete(part.page); }
  renderSelection();
});
$('#parts-jump').addEventListener('change', () => { selectionPage = Number($('#parts-jump').value); renderSelection(); });
$('#select-all').addEventListener('click', () => { selected = new Set(metadata.parts.map(part => part.page)); renderSelection(); });
$('#clear-selection').addEventListener('click', () => { selected.clear(); renderSelection(); });
$('#url').addEventListener('input', event => {
  resetVideo(); if (!$('#url').value.trim()) return;
  debounce = setTimeout(() => { inspectVideo().catch(() => {}); }, event.inputType === 'insertFromPaste' ? 0 : 600);
});
$('#form').addEventListener('submit', async event => {
  event.preventDefault(); if (submitting || (expanded && (!selected.size || selected.size > MAX_BATCH))) return;
  clearTimeout(debounce); const current = revision;
  submitting = true; updateSubmit(); $('#error').hidden = true;
  try {
    const info = await inspectVideo();
    if (!info || current !== revision) return;
    if (info.parts.length > 1 && !expanded) { showParts(info); return; }
    const chosen = info.parts.length === 1 ? info.parts : info.parts.filter(part => selected.has(part.page));
    if (!chosen.length) throw Error('请至少选择一个分 P。');
    if (chosen.length > MAX_BATCH) throw Error('每批最多选择 50 个分 P，请减少选择后分批提交。');
    await postJson('/api/jobs', {urls: chosen.map(part => part.url), title: info.title});
    if (current !== revision) { await refresh(); return; }
    if (info.parts.length === 1) { $('#url').value = ''; resetVideo(); }
    else {
      selected.clear(); renderSelection();
      $('#video-status').textContent = `已加入 ${chosen.length} 个任务，将按 P 编号依次提取。已清空勾选，可继续选择。`;
    }
    await refresh();
  } catch (error) {
    if (current === revision) showError(error);
  } finally { submitting = false; updateSubmit(); }
});
async function poll() { await refresh(); setTimeout(poll, 1200); }
poll();
