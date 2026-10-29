// Run with: node --test tests/test_okx_research_ui.cjs
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'console/research-library.js'), 'utf8');
const original = JSON.parse(fs.readFileSync(path.join(root, 'strategies/research/okx_catalog.json'), 'utf8'));

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.listeners = {}; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.text = ''; this.children = nodes; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
}

async function run(response) {
  const library = new Node('div'), summary = new Node('div');
  const calls = [];
  const document = {
    getElementById: id => id === 'okx-research-library' ? library : summary,
    createElement: tag => new Node(tag),
    createTextNode: text => Object.assign(new Node('#text'), {textContent:text})
  };
  const state = {response};
  vm.runInNewContext(script, {
    document, URL,
    fetch: async (...args) => { calls.push(args); return state.response; }
  });
  await new Promise(resolve => setImmediate(resolve));
  return {library, summary, calls, state};
}

const good = catalog => ({ok:true, json:async () => catalog});

test('renders every research card, preserves names as text, and only fetches the catalog', async () => {
  const catalog = structuredClone(original);
  catalog.strategies[0].name = '<img src=x onerror=alert(1)>';
  const {library, summary, calls} = await run(good(catalog));
  assert.equal(library.children.length, 11);
  assert.match(summary.textContent, /11 类 · 26 个截图模板/);
  assert.equal(library.children[0].children[0].children[0].textContent, catalog.strategies[0].name);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/strategies/research/okx_catalog.json');
});

test('HTTP failure is visible and retry can recover without duplicates', async () => {
  const result = await run({ok:false, status:503});
  assert.match(result.summary.textContent, /读取失败.*HTTP 503/);
  assert.equal(result.library.children.length, 0);
  result.state.response = good(original);
  await result.summary.children[0].listeners.click();
  assert.equal(result.library.children.length, 11);
  assert.match(result.summary.textContent, /26 个截图模板/);
});

test('execution-enabled entries fail closed', async () => {
  const catalog = structuredClone(original);
  catalog.strategies[0].execution_enabled = true;
  const {library, summary} = await run(good(catalog));
  assert.equal(library.children.length, 0);
  assert.match(summary.textContent, /隔离标记不符/);
});

test('non-official or executable source links never render partial cards', async () => {
  const catalog = structuredClone(original);
  catalog.sources.margin.url = 'javascript:alert(1)';
  const {library, summary} = await run(good(catalog));
  assert.equal(library.children.length, 0);
  assert.match(summary.textContent, /来源地址不符/);
});
