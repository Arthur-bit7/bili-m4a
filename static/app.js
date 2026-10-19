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
$('#form').addEventListener('submit',async event=>{
  event.preventDefault();$('#submit').disabled=true;$('#error').hidden=true;
  try{const response=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':$('meta[name="app-token"]').content},body:JSON.stringify({url:$('#url').value})});
    const result=await response.json();if(!response.ok)throw Error(result.error||'提交失败，请刷新页面后重试。');$('#url').value='';await refresh();
  }catch(error){$('#error').textContent=error.message||'连接失败，请检查本地服务。';$('#error').hidden=false;}finally{$('#submit').disabled=false;}
});
async function poll(){await refresh();setTimeout(poll,1200);}poll();
