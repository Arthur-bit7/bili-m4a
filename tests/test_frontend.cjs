const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Minimal DOM boundary: execute the real script and test user-visible flows.
class Element {
  constructor(tag='div') {
    this.tagName=tag;this.children=[];this.listeners={};this.dataset={};this.value='';
    this.hidden=false;this.disabled=false;this.textContent='';this.attributes={};this.playCalls=0;
  }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  append(...nodes) { for(const node of nodes) this.insertBefore(node,null); }
  insertBefore(node, before) {
    node.remove();const index=before ? this.children.indexOf(before) : this.children.length;
    this.children.splice(index,0,node);node.parent=this;
  }
  remove() { if(this.parent){this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null;} }
  replaceChildren(...nodes) { for(const node of [...this.children])node.remove();this.append(...nodes); }
  setAttribute(name,value) { this.attributes[name]=value; }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [ ...(selector === 'input' && child.tagName === 'input' ? [child] : []), ...child.querySelectorAll(selector) ]);
  }
  play() { this.playCalls++;return Promise.resolve(); }
}
const deferred = () => { let resolve;const promise=new Promise(r => resolve=r);return {promise,resolve}; };
const result = (body,ok=true) => ({ok,json:async () => body});
const info = (pages=2,selected=1) => ({title:'课程',selected_page:selected,parts:Array.from({length:pages},(_,i) => ({page:i+1,title:`章节 ${i+1}`,url:`https://www.bilibili.com/video/BV1bK411W797?p=${i+1}`}))});
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup() {
  const ids=['empty','count','jobs','connection','form','submit','error','url','parts','part-list','video-title','video-status',
    'queue-error','queue-status','player-panel','player-title','player','selected-count','part-search','parts-page','parts-prev','parts-next','select-page','select-all','clear-selection','parts-jump','selection-limit'];
  const elements=Object.fromEntries(ids.map(id => [id,new Element()]));
  const calls=[];const replies=[];const timers=new Map();let timerId=0;let batches=[];
  const document={createElement:tag => new Element(tag),querySelector(selector){
    if(selector.startsWith('#'))return elements[selector.slice(1)];
    if(selector.startsWith('meta'))return {content:'token'};
    throw Error(`Unexpected selector ${selector}`);
  }};
  const context=vm.createContext({document,AbortController,
    setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
    fetch:async(url,options)=>{
      if(url==='/api/batches'&&!options)return result(batches);
      calls.push({url,body:JSON.parse(options.body),signal:options.signal});
      const reply=replies.shift();if(!reply)throw Error('Missing test response');return reply.promise||reply;
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
  return {elements,calls,replies,
    input(value,inputType='insertText'){elements.url.value=value;elements.url.listeners.input({inputType});},
    async auto(){for(const[id,timer]of[...timers])if(timer.ms<=600){timers.delete(id);timer.fn();}await tick();},
    submit(){return elements.form.listeners.submit({preventDefault(){}});},
    click(id){return elements[id].listeners.click();},
    selectPage(checked){elements['select-page'].checked=checked;elements['select-page'].listeners.change();},
    jump(page){elements['parts-jump'].value=String(page);elements['parts-jump'].listeners.change();},
    select(page,checked=true){const row=elements['part-list'].children.find(row=>Number(row.children[0]?.value)===page);assert.ok(row);row.children[0].checked=checked;row.children[0].listeners.change();},
    search(query){elements['part-search'].value=query;elements['part-search'].listeners.input();},
    async batches(value){batches=value;await vm.runInContext('refresh()',context);}
  };
}
const visiblePages = ui => ui.elements['part-list'].children.filter(row=>row.children[0]?.type==='checkbox').map(row=>Number(row.children[0].value));
async function open(ui,pages=25,selected=1){ui.input('BV1bK411W797');ui.replies.push(result(info(pages,selected)));await ui.submit();}

test('preview stays collapsed; multiple selected parts submit in one batch',async()=>{
  const ui=setup();ui.input('BV1bK411W797','insertFromPaste');ui.replies.push(result(info(3,2)));await ui.auto();
  assert.equal(ui.calls.length,1);assert.equal(ui.elements.parts.hidden,true);
  await ui.submit();assert.equal(ui.calls.length,1);assert.equal(ui.elements.parts.hidden,false);
  assert.equal(ui.elements['part-list'].children[1].children[0].checked,true);
  ui.select(3);ui.replies.push(result({ids:['one','two']}));await ui.submit();
  assert.equal(ui.calls[1].url,'/api/jobs');assert.deepEqual(ui.calls[1].body.urls,info(3).parts.slice(1).map(p=>p.url));
  assert.equal(ui.elements.parts.hidden,false);
});

test('single page submits directly and clears input',async()=>{
  const ui=setup();ui.input('BV1bK411W797');ui.replies.push(result(info(1)),result({id:'job'}));await ui.submit();
  assert.equal(ui.calls.length,2);assert.equal(ui.elements.url.value,'');assert.equal(ui.elements.parts.hidden,true);
});

test('selection pages contain five items and preserve checks across search and direct jumps',async()=>{
  const ui=setup();await open(ui,25,23);assert.deepEqual(visiblePages(ui),[21,22,23,24,25]);
  assert.equal(ui.elements['parts-jump'].value,'5');
  assert.equal(ui.elements['part-list'].children[2].children[0].checked,true);
  ui.click('parts-prev');assert.deepEqual(visiblePages(ui),[16,17,18,19,20]);
  ui.select(17);ui.search('章节 1');assert.equal(visiblePages(ui).length,5);
  assert.equal(ui.elements['parts-jump'].value,'1');
  ui.search('');assert.deepEqual(visiblePages(ui),[1,2,3,4,5]);
  ui.jump(4);assert.equal(ui.elements['part-list'].children[1].children[0].checked,true);
  assert.ok(ui.elements['selected-count'].textContent.startsWith('已选 2 个（本页 1 个）'));
  assert.ok(ui.elements['selected-count'].textContent.includes('其他页或隐藏结果'));
});

test('page checkbox supports partial, all and none without clearing other pages',async()=>{
  const ui=setup();await open(ui,12,1);
  assert.equal(ui.elements['select-page'].checked,false);assert.equal(ui.elements['select-page'].indeterminate,true);
  ui.selectPage(true);assert.equal(ui.elements['select-page'].checked,true);assert.equal(ui.elements['select-page'].indeterminate,false);
  ui.jump(2);ui.select(6);ui.jump(1);ui.selectPage(false);
  assert.ok(ui.elements['selected-count'].textContent.startsWith('已选 1 个（本页 0 个）'));
  ui.jump(2);assert.equal(ui.elements['part-list'].children[0].children[0].checked,true);
});

test('page selection is scoped to current filtered entries, clear empties all pages',async()=>{
  const ui=setup();await open(ui);ui.click('clear-selection');ui.search('章节 2');ui.selectPage(true);
  assert.ok(ui.elements['selected-count'].textContent.startsWith('已选 5 个（本页 5 个）'));
  ui.search('');ui.click('clear-selection');assert.ok(ui.elements['selected-count'].textContent.startsWith('已选 0 个'));
  assert.equal(ui.elements.submit.disabled,true);
  await ui.submit();assert.equal(ui.calls.length,1);
});

test('selecting more than fifty immediately explains limit and disables submission',async()=>{
  const ui=setup();await open(ui,60);ui.click('select-all');
  assert.ok(ui.elements['selected-count'].textContent.includes('60'));assert.equal(ui.elements.submit.disabled,true);
  assert.equal(ui.elements['selection-limit'].hidden,false);assert.ok(ui.elements['selection-limit'].textContent.includes('50'));
  await ui.submit();assert.equal(ui.calls.length,1);
  ui.click('clear-selection');ui.select(1);assert.equal(ui.elements.submit.disabled,false);
  assert.equal(ui.elements['selection-limit'].hidden,true);
});

test('submit feedback freezes selection; success clears checks but preserves page and filter',async()=>{
  const ui=setup();await open(ui,25,23);ui.search('章节 2');ui.jump(2);ui.select(24);
  const before=ui.elements['parts-jump'].value;const response=deferred();ui.replies.push(response);
  const submit=ui.submit();await tick();assert.equal(ui.elements.submit.textContent,'正在提交…');
  assert.equal(ui.elements['part-search'].disabled,true);assert.equal(ui.elements['select-page'].disabled,true);
  response.resolve(result({ids:['one']}));await submit;
  assert.ok(ui.elements['selected-count'].textContent.startsWith('已选 0 个'));assert.equal(ui.elements['part-search'].value,'章节 2');
  assert.equal(ui.elements['parts-jump'].value,before);assert.equal(ui.elements['part-search'].disabled,false);
  assert.equal(ui.elements.submit.disabled,true);assert.ok(ui.elements['video-status'].textContent.includes('已清空勾选'));
});

test('failed submission keeps selection and re-enables retry',async()=>{
  const ui=setup();await open(ui,12,7);ui.replies.push(result({error:'队列已满'},false));await ui.submit();
  assert.ok(ui.elements['selected-count'].textContent.startsWith('已选 1 个'));assert.equal(ui.elements.submit.disabled,false);
  assert.equal(ui.elements.error.textContent,'队列已满');assert.equal(ui.elements['parts-jump'].value,'2');
});

test('enter in search does not submit a download',async()=>{
  const ui=setup();await open(ui);let prevented=false;
  ui.elements['part-search'].listeners.keydown({key:'Enter',preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(ui.calls.length,1);
});

test('click while preview is pending reuses that request',async()=>{
  const ui=setup();const reply=deferred();ui.replies.push(reply);ui.input('BV1bK411W797');await ui.auto();
  const submit=ui.submit();assert.equal(ui.calls.length,1);reply.resolve(result(info()));await submit;
  assert.equal(ui.calls.length,1);assert.equal(ui.elements.parts.hidden,false);
});

test('old response cannot overwrite a changed link or create a job',async()=>{
  const ui=setup();const old=deferred();ui.replies.push(old);ui.input('old');await ui.auto();const submit=ui.submit();
  ui.input('new');ui.replies.push(result(info(1)));await ui.auto();old.resolve(result(info(3)));await submit;
  assert.equal(ui.calls.length,2);assert.equal(ui.elements.parts.hidden,true);assert.ok(ui.elements['video-status'].textContent.includes('单 P'));
});

test('failed preview retries and double submit cannot duplicate a batch',async()=>{
  const ui=setup();ui.input('BV1bK411W797');ui.replies.push(result({error:'network failed'},false));await ui.auto();
  assert.equal(ui.elements['video-status'].textContent,'network failed');
  ui.replies.push(result(info(1)));const job=deferred();ui.replies.push(job);const first=ui.submit();await tick();
  await ui.submit();assert.equal(ui.calls.length,3);job.resolve(result({id:'job'}));await first;
});

test('rapid typing is debounced',async()=>{
  const ui=setup();ui.input('B');ui.input('BV1');ui.input('BV1bK411W797');ui.replies.push(result(info(1)));await ui.auto();
  assert.equal(ui.calls.length,1);
});

const batch = () => ({id:'batch',title:'课程',total:12,created:'12:00',jobs:Array.from({length:12},(_,i)=>({
  id:`job${i+1}`,page:i+1,title:`章节 ${i+1}`,status:'done',duration:60,size:1024,download:`/audio/job${i+1}`
}))});
test('task pages contain five rows; direct jumps and polling preserve pager and playback',async()=>{
  const ui=setup();const value=batch();await ui.batches([value]);
  const card=ui.elements.jobs.children[0];assert.equal(card.children[2].children.length,5);
  await card.children[2].children[0].children[0].children[1].children[0].listeners.click();
  const player=ui.elements.player;assert.equal(player.src,'/audio/job1');assert.equal(player.playCalls,1);
  const pager=card.children[3];const jump=pager.controls.select;jump.value='3';jump.listeners.change();
  assert.equal(card.children[2].children.length,2);
  await ui.batches([{...value,created:'12:01'}]);assert.equal(card.children[2].children.length,2);
  assert.equal(card.children[3],pager);assert.equal(pager.controls.select,jump);assert.equal(jump.value,'3');
  assert.equal(ui.elements.player,player);assert.equal(player.src,'/audio/job1');assert.equal(player.playCalls,1);
  await ui.batches([]);assert.equal(ui.elements.jobs.children.length,0);
});

test('cancel controls target queued jobs and batches',async()=>{
  const ui=setup();const value=batch();value.jobs[0].status='queued';value.jobs[0].progress=null;
  await ui.batches([value]);const card=ui.elements.jobs.children[0];
  ui.replies.push(result({cancelled:1}));await card.children[2].children[0].children[0].children[1].children[0].listeners.click();
  assert.equal(ui.calls[0].url,'/api/jobs/job1/cancel');
  ui.replies.push(result({cancelled:1}));await card.children[0].children[1].listeners.click();
  assert.equal(ui.calls[1].url,'/api/batches/batch/cancel');
});
