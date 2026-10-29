// DOM test double + actual Python JSON CLI. This does not test a native renderer.
const fs = require("fs");
const vm = require("vm");
const readline = require("readline");
const assert = require("assert");
const html = fs.readFileSync("spike.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

const pending = [];
readline.createInterface({input:process.stdin}).on("line", line => {
  const resolve = pending.shift();
  if (resolve) resolve(JSON.parse(line));
});
function send(type, payload) {
  return new Promise(resolve => { pending.push(resolve); process.stdout.write(JSON.stringify({type,payload}) + "\n"); });
}
function environment() {
  function api(request) { return send("request", request); }
  const elements = {};
  function element(id) {
    if (!elements[id]) {
      const classes = new Set();
      elements[id] = {id, hidden:false, disabled:false, textContent:"", attrs:{}, classList:{toggle(name, value) {if (value) classes.add(name); else classes.delete(name);}, contains(name){return classes.has(name);}}, setAttribute(key,value){this.attrs[key] = value;}};
    }
    return elements[id];
  }
  const events = {};
  const window = {pywebview:{api:{request:api}}, addEventListener(name, fn){events[name] = fn;}};
  const timer = (fn, ms) => {const t = setTimeout(fn, ms); if (ms === 7000) t.unref(); return t;};
  vm.runInNewContext(script, {window, document:{getElementById:element}, setTimeout:timer, Error, JSON, Boolean, String});
  events.pywebviewready();
  return {element, api};
}
async function waitFor(predicate) {
  for (let i=0;i<200;i++) {if (predicate()) return; await delay(10);}
  throw new Error("timeout waiting for UI state");
}
async function click(env, id) {
  assert.strictEqual(env.element(id).disabled, false, id + " is disabled");
  await env.element(id).onclick();
}
async function stop() { await send("restart", null); }
(async () => {
  const e = environment();
  await waitFor(() => e.element("message").textContent.includes("мост готов"));
  await click(e,"counter");
  assert.match(e.element("message").textContent, /не показания/);
  await click(e,"snapshot");
  await click(e,"fixture");
  assert.match(e.element("message").textContent, /Не является показанием реальной установки/);
  await click(e,"stand");
  assert.match(e.element("message").textContent, /модель не подтверждена/);
  await click(e,"tab-air");
  assert.strictEqual(e.element("heat-svg").hidden, true);
  assert.strictEqual(e.element("air-svg").hidden, false);
  await e.element("air-area").onclick();
  assert.strictEqual(e.element("point").classList.contains("selected"), true);
  assert.match(e.element("message").textContent, /Выбрана условная точка/);
  await click(e,"help"); assert.match(e.element("message").textContent, /без физических координат/);
  await click(e,"properties"); assert.match(e.element("message").textContent, /модель не утверждена/);
  await click(e,"save"); assert.match(e.element("message").textContent, /Сеанс сохранён/);
  const call = (op,args) => e.api({api_version:"0.1",request_id:"external",op,args});
  const old = await call("storage.read",{target:"spike-air.json"});
  assert.strictEqual(old.outcome,"ok");
  const changed = await call("storage.write",{target:"spike-air.json",document:old.data.document,expected_generation:old.data.generation});
  assert.strictEqual(changed.outcome,"ok");
  await click(e,"save"); assert.match(e.element("message").textContent, /изменён другим экземпляром/);
  await stop(e);
  const f = environment();
  await waitFor(() => f.element("message").textContent.includes("мост готов"));
  await click(f,"tab-air"); await click(f,"restore");
  assert.match(f.element("message").textContent, /Сеанс восстановлен/);
  assert.strictEqual(f.element("point").classList.contains("selected"),true);
  await stop(f);
  process.stdout.write(JSON.stringify({type:"done"}) + "\n");
})().catch(error => {console.error(error); process.exitCode=1;});
