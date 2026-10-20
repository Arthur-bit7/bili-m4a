const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// A small DOM boundary lets the real page script exercise asynchronous input flows.
class Element {
  constructor() { this.children=[]; this.listeners={}; this.value=''; this.hidden=false; this.disabled=false; this.textContent=''; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
}
const deferred = () => { let resolve; const promise=new Promise(r => resolve=r); return {promise, resolve}; };
const result = (body, ok=true) => ({ok, json:async () => body});
const info = (pages=2, selected=1) => ({title:'课程', selected_page:selected, parts:Array.from({length:pages}, (_,i) => ({page:i+1,title:`章节 ${i+1}`,url:`https://www.bilibili.com/video/BV1bK411W797?p=${i+1}`}))});
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup() {
  const ids=['empty','count','jobs','connection','form','submit','error','url','parts','part-list','video-title','video-status'];
  const elements=Object.fromEntries(ids.map(id => [id,new Element()]));
  const calls=[]; const replies=[]; const timers=new Map(); let timerId=0;
  const document={
    createElement:() => new Element(),
    querySelector(selector) {
      if(selector.startsWith('#')) return elements[selector.slice(1)];
      if(selector.startsWith('meta')) return {content:'token'};
      if(selector==='input[name="part"]:checked') return elements['part-list'].children.flatMap(row => row.children).find(el => el.checked);
      throw Error(`Unexpected selector ${selector}`);
    }
  };
  const context=vm.createContext({document, AbortController,
    setTimeout:(fn,ms) => {const id=++timerId;timers.set(id,{fn,ms});return id;},
    clearTimeout:id => timers.delete(id),
    fetch:async (url,options) => {
      if(url==='/api/jobs' && !options) return result([]);
      calls.push({url,body:JSON.parse(options.body),signal:options.signal});
      const reply=replies.shift(); if(!reply) throw Error('Missing test response');
      return reply.promise ? reply.promise : reply;
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'), context);
  return {elements,calls,replies,
    input(value, inputType='insertText') {elements.url.value=value;elements.url.listeners.input({inputType});},
    async auto() {for(const [id,timer] of [...timers]) if(timer.ms<=600){timers.delete(id);timer.fn();} await tick();},
    submit() {return elements.form.listeners.submit({preventDefault(){}});},
    select(page) {for(const row of elements['part-list'].children) row.children[0].checked=Number(row.children[0].value)===page;}
  };
}

test('automatic preview stays collapsed, then single selection submits only the chosen page',async () => {
  const ui=setup();ui.input('BV1bK411W797','insertFromPaste');ui.replies.push(result(info(3,2)));
  await ui.auto();assert.equal(ui.calls.length,1);assert.equal(ui.elements.parts.hidden,true);
  await ui.submit();assert.equal(ui.calls.length,1);assert.equal(ui.elements.parts.hidden,false);
  assert.equal(ui.elements['part-list'].children[1].children[0].checked,true);
  ui.select(3);ui.replies.push(result({id:'job'}));await ui.submit();
  assert.equal(ui.calls[1].url,'/api/jobs');assert.ok(ui.calls[1].body.url.endsWith('?p=3'));
  assert.equal(ui.elements.parts.hidden,false);assert.equal(ui.elements.url.value,'BV1bK411W797');
});

test('single page submits directly and resets the input',async () => {
  const ui=setup();ui.input('BV1bK411W797');ui.replies.push(result(info(1)),result({id:'job'}));
  await ui.submit();assert.equal(ui.calls.length,2);assert.equal(ui.elements.url.value,'');assert.equal(ui.elements.parts.hidden,true);
});

test('click while preview is pending reuses that request',async () => {
  const ui=setup();const reply=deferred();ui.replies.push(reply);ui.input('BV1bK411W797');await ui.auto();
  const submit=ui.submit();assert.equal(ui.calls.length,1);reply.resolve(result(info()));await submit;
  assert.equal(ui.calls.length,1);assert.equal(ui.elements.parts.hidden,false);
});

test('old preview cannot overwrite the new input or submit a job',async () => {
  const ui=setup();const old=deferred();ui.replies.push(old);ui.input('old');await ui.auto();const submit=ui.submit();
  ui.input('new');ui.replies.push(result({...info(1),title:'new video'}));await ui.auto();
  old.resolve(result(info(3)));await submit;
  assert.equal(ui.calls.length,2);assert.equal(ui.elements.parts.hidden,true);
  assert.ok(ui.elements['video-status'].textContent.includes('单 P'));
});

test('failed preview is retried on click without submitting a download',async () => {
  const ui=setup();ui.input('BV1bK411W797');ui.replies.push(result({error:'network failed'},false));await ui.auto();
  assert.equal(ui.elements['video-status'].textContent,'network failed');
  ui.replies.push(result(info()));await ui.submit();assert.equal(ui.calls.length,2);assert.equal(ui.elements.parts.hidden,false);
});

test('rapid typing is debounced and double submit cannot duplicate a job',async () => {
  const ui=setup();ui.input('B');ui.input('BV1');ui.input('BV1bK411W797');ui.replies.push(result(info(1)));await ui.auto();
  assert.equal(ui.calls.length,1);const job=deferred();ui.replies.push(job);
  const first=ui.submit();await tick();await ui.submit();assert.equal(ui.calls.length,2);
  job.resolve(result({id:'job'}));await first;
});
