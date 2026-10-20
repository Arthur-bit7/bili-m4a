const $ = selector => document.querySelector(selector);
const cards = new Map();
const labels = {queued:'排队中',extracting:'解析中',downloading:'下载中',converting:'校验中',done:'已完成',error:'未完成'};
function text(tag, value, className) { const node=document.createElement(tag); node.textContent=value; if(className)node.className=className; return node; }
function render(jobs) {
  $('#empty').hidden=jobs.length>0; $('#count').textContent=`${jobs.length} 个任务`;
  for (const job of jobs) {
    let card=cards.get(job.id);
    if (!card) {card=document.createElement('article');card.className='job';cards.set(job.id,card);$('#jobs').prepend(card);}
    if(card.dataset.signature===JSON.stringify(job))continue;
    card.dataset.signature=JSON.stringify(job);
    card.classList.toggle('error-state',job.status==='error');
    card.replaceChildren();
    const top=text('div','','job-top');top.append(text('h3',job.title));
    if(job.status==='done') {const link=text('a','下载 M4A','download');link.href=job.download+'?download=1';top.append(link);}
    card.append(top);
    const detail=job.status==='done' ? `${Math.floor(job.duration/60)}:${String(Math.floor(job.duration%60)).padStart(2,'0')} · ${(job.size/1048576).toFixed(1)} MB · M4A` : job.message;
    card.append(text('p',`${job.created} · ${labels[job.status]} · ${detail}`));
    if(job.status==='done'){const audio=document.createElement('audio');audio.controls=true;audio.preload='none';audio.src=job.download;audio.setAttribute('aria-label',job.title);card.append(audio);}
    else if(job.status!=='error'){const progress=document.createElement('progress');progress.max=100;if(job.progress!==null)progress.value=job.progress;progress.setAttribute('aria-label','提取进度');card.append(progress);}
  }
  // Keep server order without recreating completed audio players.
  jobs.forEach((job,index)=>{const node=cards.get(job.id);if($('#jobs').children[index]!==node)$('#jobs').insertBefore(node,$('#jobs').children[index]||null);});
}
async function refresh(){
  try {const response=await fetch('/api/jobs');if(!response.ok)throw Error();render(await response.json());$('#connection').hidden=true;}
  catch {$('#connection').textContent='暂时无法连接本地服务，请确认启动窗口仍在运行。';$('#connection').hidden=false;}
}
// Metadata belongs to one exact input revision; stale responses cannot update it.
let revision = 0;
let metadata = null;
let pending = null;
let controller = null;
let debounce = null;
let expanded = false;

function resetVideo() {
  revision++;
  clearTimeout(debounce);
  if (controller) controller.abort();
  controller = null;
  pending = null;
  metadata = null;
  expanded = false;
  $('#parts').hidden = true;
  $('#part-list').replaceChildren();
  $('#submit').textContent = '获取音频 ↗';
  $('#error').hidden = true;
  $('#video-status').textContent = $('#url').value.trim() ? '等待识别…' : '粘贴链接后自动识别分 P';
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

function showParts(info) {
  $('#part-list').replaceChildren();
  $('#video-title').textContent = info.title;
  for (const part of info.parts) {
    const row = document.createElement('label');
    row.className = 'part-option';
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'part'; radio.value = part.page;
    radio.checked = part.page === info.selected_page;
    row.append(radio, text('span', `P${part.page} · ${part.title}`));
    $('#part-list').append(row);
  }
  expanded = true;
  $('#parts').hidden = false;
  $('#submit').textContent = '提取所选分 P ↗';
  $('#video-status').textContent = `共 ${info.parts.length} 个分 P，每次提取一个；提交后可继续选择其他分 P。`;
}

$('#url').addEventListener('input', event => {
  resetVideo();
  if (!$('#url').value.trim()) return;
  // Paste can be identified immediately; typing waits until the input settles.
  debounce = setTimeout(() => { inspectVideo().catch(() => {}); }, event.inputType === 'insertFromPaste' ? 0 : 600);
});

$('#form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('#submit').disabled) return;
  clearTimeout(debounce);
  const current = revision;
  $('#submit').disabled = true;
  $('#error').hidden = true;
  try {
    const info = await inspectVideo();
    if (!info || current !== revision) return;
    if (info.parts.length > 1 && !expanded) { showParts(info); return; }
    const selected = info.parts.length === 1 ? info.parts[0]
      : info.parts.find(part => part.page === Number($('input[name="part"]:checked')?.value));
    if (!selected) throw Error('请选择一个分 P。');
    await postJson('/api/jobs', {url: selected.url});
    if (current !== revision) { await refresh(); return; }
    if (info.parts.length === 1) { $('#url').value = ''; resetVideo(); }
    else { $('#video-status').textContent = `P${selected.page} 已加入任务，可继续选择其他分 P。`; }
    await refresh();
  } catch (error) {
    if (current !== revision) return;
    $('#error').textContent = error.message || '连接失败，请检查本地服务。';
    $('#error').hidden = false;
  } finally {
    $('#submit').disabled = false;
  }
});
async function poll(){await refresh();setTimeout(poll,1200);}poll();
