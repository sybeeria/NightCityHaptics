/* Run the control page's real request queue against a simulated HTTP server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/companion/ui.html'), 'utf8');
const helpers = source.slice(source.indexOf('const settingsQueue'), source.indexOf('function fill'));
const turn = () => new Promise(resolve => setImmediate(resolve));

function page(fetch) {
  const context = vm.createContext({
    S: {settings: {enabled: true, max_strength: .7, interactions: {}, devices: {}}, devices: []},
    update() {}, $() { return {textContent: '', className: ''}; },
    AbortController, setTimeout, clearTimeout, fetch,
  });
  vm.runInContext(helpers, context);
  return {
    api: (action, body) => { context.action = action; context.body = body; return vm.runInContext('api(action, body)', context); },
    eval: code => vm.runInContext(code, context),
  };
}
const ok = () => ({ok: true, json: async () => ({ok: true})});

async function offPreemptsOlderClicks() {
  const calls = [];
  let release;
  const p = page(async (url, options) => {
    calls.push({action: url.split('/').pop(), body: JSON.parse(options.body)});
    if (calls.length === 1) await new Promise(resolve => { release = resolve; });
    return ok();
  });
  const writes = [p.api('settings', {max_strength: .2}),
    p.api('settings', {interactions: {hurt: {strength: .4}}}),
    p.api('settings', {enabled: true}), p.api('test-device', {key: 'simulated'}),
    p.api('try', {id: 'hurt'}), p.api('settings', {enabled: false})];
  assert.equal(calls.length, 1);
  release();
  const replies = await Promise.all(writes);
  await turn();
  assert.deepEqual(calls, [
    {action: 'settings', body: {max_strength: .2}},
    {action: 'settings', body: {enabled: false}},
    {action: 'settings', body: {interactions: {hurt: {strength: .4}}}},
  ]);
  assert.equal(replies.filter(r => r.canceled).length, 3);
  assert.equal(p.eval('pendingSettings'), 0);
  assert.equal(p.eval('S.settings.enabled'), false);
}

async function newIntentAfterOffStillWorks() {
  const calls = [];
  const p = page(async (url, options) => {calls.push([url.split('/').pop(), JSON.parse(options.body)]); return ok();});
  await p.api('settings', {enabled: false});
  await p.api('test-device', {key: 'simulated'});
  assert.equal(p.eval('S.settings.enabled'), false);
  await p.api('settings', {enabled: true});
  assert.deepEqual(calls, [['settings', {enabled: false}], ['test-device', {key: 'simulated'}], ['settings', {enabled: true}]]);
  assert.equal(p.eval('pendingSettings'), 0);
}

async function failuresDoNotWedgeOrPreview() {
  const calls = [];
  const p = page(async (url) => {
    calls.push(url.split('/').pop());
    if (calls.length === 1) throw new Error('simulated disk failure');
    return ok();
  });
  const results = await Promise.allSettled([p.api('settings', {max_strength: .3}), p.api('try', {id: 'hurt'})]);
  assert.deepEqual(results.map(r => r.status), ['rejected', 'rejected']);
  assert.deepEqual(calls, ['settings']);
  assert.equal(p.eval('pendingSettings'), 0);
  // A subsequent successful state poll reconciles rejected optimistic settings.
  p.eval('settingsFailed = false');
  await p.api('settings', {enabled: false});
  assert.deepEqual(calls, ['settings', 'settings']);
}

async function quitDoesNotWaitAndPreviewFlushesDebounce() {
  const calls = [];
  let release;
  const p = page(async (url, options) => {
    const action = url.split('/').pop();
    calls.push([action, JSON.parse(options.body)]);
    if (action === 'settings' && calls.length === 1) await new Promise(resolve => { release = resolve; });
    return ok();
  });
  const writing = p.api('settings', {max_strength: .3});
  await p.api('quit');
  assert.deepEqual(calls.map(c => c[0]), ['settings', 'quit']);
  release(); await writing; await turn();
  p.eval("save({max_strength:.1}, 'master', 1000)");
  await p.api('try', {id: 'hurt'}); await turn();
  assert.deepEqual(calls.slice(-2), [['settings', {max_strength: .1}], ['try', {id: 'hurt'}]]);
  assert.equal(p.eval('pendingSettings'), 0);
  assert.equal(p.eval('pendingSaves.size'), 0);
}

function twoDevices(p) {
  p.eval("S.devices=[{key:'device:1',settings_key:'saved:1',settings:{on:true,strength:1,outputs:{heat:true,vibrate:true}}},{key:'device:2',settings_key:'saved:2',settings:{on:true,strength:1,outputs:{heat:true,vibrate:true}}}]; S.settings.devices={'saved:1':{on:true,strength:1,outputs:{heat:true,vibrate:true}},'saved:2':{on:true,strength:1,outputs:{heat:true,vibrate:true}}};");
}
async function deviceOffRetiresOnlyItsOlderEnablesAndTests() {
  const calls=[]; let release;
  const p=page(async (url,options)=>{
    calls.push([url.split('/').pop(),JSON.parse(options.body)]);
    if(calls.length===1) await new Promise(resolve=>{release=resolve;});
    return ok();
  });
  twoDevices(p);
  const writes=[p.api('settings',{max_strength:.3}),
    p.api('test-device',{key:'device:1'}), p.api('test-device',{key:'device:2'}),
    p.api('settings',{devices:{'saved:1':{on:true,strength:.4},'saved:2':{on:true}}}),
    p.api('settings',{devices:{'saved:1':{on:false}}})];
  release(); const replies=await Promise.all(writes); await turn();
  assert.deepEqual(calls,[['settings',{max_strength:.3}],['settings',{devices:{'saved:1':{on:false}}}],
    ['test-device',{key:'device:2'}],['settings',{devices:{'saved:1':{strength:.4},'saved:2':{on:true}}}]]);
  assert.equal(replies.filter(reply=>reply.canceled).length,1);
  assert.equal(p.eval('S.settings.devices["saved:1"].on'),false);
  assert.equal(p.eval('pendingSettings'),0);
}
async function channelOffCannotBeReenabledByOlderQueuedClicks() {
  const calls=[]; let release;
  const p=page(async (url,options)=>{
    calls.push([url.split('/').pop(),JSON.parse(options.body)]);
    if(calls.length===1) await new Promise(resolve=>{release=resolve;});
    return ok();
  });
  twoDevices(p);
  const writes=[p.api('settings',{max_strength:.3}),p.api('test-device',{key:'device:1'}),
    p.api('settings',{devices:{'saved:1':{outputs:{heat:true,vibrate:false}},'saved:2':{outputs:{heat:true}}}}),
    p.api('settings',{devices:{'saved:1':{outputs:{heat:false}}}})];
  release(); await Promise.all(writes); await turn();
  assert.deepEqual(calls,[['settings',{max_strength:.3}],['settings',{devices:{'saved:1':{outputs:{heat:false}}}}],
    ['settings',{devices:{'saved:1':{outputs:{vibrate:false}},'saved:2':{outputs:{heat:true}}}}]]);
  assert.equal(p.eval('S.settings.devices["saved:1"].outputs.heat'),false);
  assert.equal(p.eval('pendingSettings'),0);
}
async function deviceIntentAfterOffStillWorks() {
  const calls=[];
  const p=page(async (url,options)=>{calls.push([url.split('/').pop(),JSON.parse(options.body)]);return ok();});
  twoDevices(p);
  await p.api('settings',{devices:{'saved:1':{on:false}}});
  await p.api('test-device',{key:'device:1'});
  await p.api('settings',{devices:{'saved:1':{on:true}}});
  assert.deepEqual(calls,[['settings',{devices:{'saved:1':{on:false}}}],['test-device',{key:'device:1'}],
    ['settings',{devices:{'saved:1':{on:true}}}]]);
  assert.equal(p.eval('pendingSettings'),0);
}

(async () => {
  for (const test of [offPreemptsOlderClicks, newIntentAfterOffStillWorks, failuresDoNotWedgeOrPreview, quitDoesNotWaitAndPreviewFlushesDebounce,
    deviceOffRetiresOnlyItsOlderEnablesAndTests, channelOffCannotBeReenabledByOlderQueuedClicks, deviceIntentAfterOffStillWorks]) {
    await test(); console.log('PASS ' + test.name);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
