/** Deterministic browser fixture/accounting checks. Run: node tests/frontend.mjs */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { build, transform } from 'esbuild';
const bundled = await build({ entryPoints: ['src/data.ts'], bundle: true, write: false, format: 'esm', platform: 'node' });
const data = await import('data:text/javascript;base64,' + Buffer.from(bundled.outputFiles[0].text).toString('base64'));
let passed = 0;
function check(name, fn) { fn(); passed++; console.log(`PASS ${name}`); }
for (const market of data.markets) {
  for (const interval of ['1m', '15m', '1h', '1d']) {
    check(`fixture ${market.symbol} ${interval}: deterministic, ordered, valid OHLCV`, () => {
      const a = data.makeCandles(market.symbol, interval);
      assert.deepEqual(a, data.makeCandles(market.symbol, interval));
      assert.equal(a.length, 120);
      assert(Math.abs(a.at(-1).close - market.price) < 1e-8);
      for (let i = 0; i < a.length; i++) {
        const c = a[i];
        assert(c.low <= Math.min(c.open, c.close) && c.high >= Math.max(c.open, c.close) && c.volume > 0);
        if (i) assert(c.time > a[i - 1].time);
      }
    });
  }
}
const context = { ...data, console, crypto: globalThis.crypto, localStorage: { setItem() {} }, setInterval() {}, AbortSignal, fetch, URL, URLSearchParams };
vm.createContext(context);
const prepareSource = text => text.replace(/\r\n?/g, '\n').replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
check('VM source preparation handles Windows CRLF and Unix LF', () => {
  const sample = 'import { useEffect } from "react";\nexport const value = 1;\n';
  assert.equal(prepareSource(sample), prepareSource(sample.replaceAll('\n', '\r\n')));
  assert(!prepareSource(sample).includes('import '));
});
const source = prepareSource(fs.readFileSync('src/api.ts', 'utf8'));
const compiled = await transform(source, { loader: 'ts', target: 'es2022' });
vm.runInContext(compiled.code + ';globalThis.test={executePaper,localBacktest,revalue,actions,getInstruments,getState:()=>state,setState:s=>{state=s;},setConnection:c=>{connection=c;connected=!!c;},fixtureTick};', context);
const { executePaper, localBacktest, actions } = context.test;
check('paper round trip debits fees and slippage and closes position', () => {
  const start = data.createInitialState();
  const bought = executePaper(start, { symbol: 'SOL/USD', side: 'buy', qty: 1, price: 100 });
  assert(Math.abs(bought.cash - (start.cash - 100.15005)) < 1e-7);
  const sold = executePaper(bought, { symbol: 'SOL/USD', side: 'sell', qty: 1, price: 100 });
  assert(!sold.positions.find(p => p.symbol === 'SOL/USD'));
  assert(sold.cash < start.cash);
  assert.equal(sold.orders.length, 2);
});
check('paper execution rejects overspend, oversell, nonfinite values, and excessive exposure', () => {
  const start = data.createInitialState();
  assert.throws(() => executePaper(start, { symbol: 'BTC/USD', side: 'buy', qty: 1000, price: 67432 }), /Insufficient/);
  assert.throws(() => executePaper(start, { symbol: 'SOL/USD', side: 'sell', qty: 1, price: 100 }), /exceeds/);
  assert.throws(() => executePaper(start, { symbol: 'SOL/USD', side: 'buy', qty: NaN, price: 100 }), /finite/);
  assert.throws(() => executePaper({ ...start, risk: { ...start.risk, maxExposure: .1 } }, { symbol: 'SOL/USD', side: 'buy', qty: 1, price: 100 }), /exposure/);
});
check('backtest is deterministic with valid equity, drawdowns and fees', () => {
  const config = { symbol: 'BTC/USD', interval: '1h', threshold: .6 };
  const result = localBacktest(config);
  assert.deepEqual(result, localBacktest(config));
  assert(result.equity.length > 100);
  assert(Number.isFinite(result.finalEquity));
  assert(result.maxDrawdown >= 0);
  assert(result.trades.length > 0 && result.trades.every(t => t.fee > 0));
  assert(result.tradeCount <= result.trades.length / 2);
});
check('backtest rejects invalid parameters and future candles cannot change earlier trades', () => {
  assert.throws(() => localBacktest({ symbol: 'BTC/USD', interval: '1h', threshold: NaN }), /finite/);
  const candles = data.makeCandles('BTC/USD', '1h', 240);
  context.makeCandles = () => candles;
  const baseline = localBacktest({ symbol: 'BTC/USD', interval: '1h', threshold: .6 });
  const cutoff = candles[160].time;
  context.makeCandles = () => candles.map((c, i) => i < 160 ? c : { ...c, open: c.open * 1.7, high: c.high * 1.7, low: c.low * 1.7, close: c.close * 1.7 });
  const changed = localBacktest({ symbol: 'BTC/USD', interval: '1h', threshold: .6 });
  const earlier = result => result.trades.filter(t => Date.parse(t.time) / 1000 < cutoff);
  assert.deepEqual(earlier(baseline), earlier(changed));
  context.makeCandles = data.makeCandles;
});
await actions.setMode('live');
await actions.submitOrder({ symbol: 'BTC/USD', side: 'buy', qty: .01, price: 67432.8 });
const proposal = context.test.getState().orders[0];
assert.equal(proposal.status, 'pending_review');
await assert.rejects(actions.confirmOrder(proposal.id), /local desktop engine/);
await actions.cancelOrder(proposal.id);
assert.equal(context.test.getState().orders[0].status, 'cancelled');
passed++;
console.log('PASS preview live workflow creates/cancels proposals and cannot submit real orders');
await actions.reset();
await actions.connectProvider({ provider: 'alpaca', key: 'never-persist', secret: 'never-persist' }).then(() => assert.fail('Preview must reject credentials'), e => assert.match(e.message, /local desktop app/));
passed++;
console.log('PASS preview refuses account secrets before storage or network access');
check('disabled strategies retain independent paper risk stops during fixture replay', () => {
  const start = data.createInitialState();
  start.running = true;
  start.strategies = start.strategies.map(s => ({ ...s, enabled: false }));
  start.positions = start.positions.map(p => p.symbol === 'BTC/USD' ? { ...p, avgPrice: 100000 } : p);
  context.test.setState(start);
  context.test.fixtureTick();
  assert(!context.test.getState().positions.some(p => p.symbol === 'BTC/USD'));
  assert(context.test.getState().orders.some(o => o.symbol === 'BTC/USD' && o.side === 'sell'));
});
await actions.reset();
await assert.rejects(actions.saveModel({ name: 'Local endpoint', endpoint: 'http://127.0.0.1:8081/predict', version: 'v1', timeout: 5, kind: 'http', status: 'unknown', apiKey: 'never-store-this' }), /never stored/);
assert(!context.test.getState().models.some(m => 'apiKey' in m || 'api_key' in m));
passed++;
console.log('PASS browser model registration refuses secrets before persistence');
let lastModelPayload, lastConfirmationPayload, lastProposalPayload, lastPaperPayload;
context.fetch = async (url, options) => {
  if (url.endsWith('/models')) lastModelPayload = JSON.parse(options.body);
  if (url.includes('/confirm')) lastConfirmationPayload = JSON.parse(options.body);
  if (url.endsWith('/proposals')) lastProposalPayload = JSON.parse(options.body);
  if (url.endsWith('/orders')) lastPaperPayload = JSON.parse(options.body);
  if (url.includes('/instruments?')) return { ok: true, json: async () => ({ instruments: [{ symbol: 'BTC/USD', name: 'Bitcoin', class: 'crypto' }, { symbol: 'ZZZZ', name: 'Sample equity', asset_class: 'us_equity' }] }) };
  return { ok: true, json: async () => url.endsWith('/snapshot') ? { cash: 100000, equity: 100000, initial_cash: 100000, positions: [], orders: [], signals: [], strategies: [], proposals: [], mode: 'live', models: [{ id: 'existing-model', name: 'Endpoint', endpoint: 'http://127.0.0.1:8081/predict', status: 'unknown' }], data_source: 'alpaca' } : {} };
};
context.test.setConnection({ url: 'http://127.0.0.1:43210', token: 'test-session-token' });
await actions.saveModel({ id: 'existing-model', name: 'Endpoint', endpoint: 'http://127.0.0.1:8081/predict', version: 'v2', timeout: 5, kind: 'http', status: 'unknown', apiKey: 'test-vault-secret' });
assert.equal(lastModelPayload.id, 'existing-model');
assert.equal(lastModelPayload.api_key, 'test-vault-secret');
assert.equal(context.test.getState().models[0].kind, 'http');
assert(!('apiKey' in context.test.getState().models[0]));
await actions.confirmOrder('test-proposal');
assert.deepEqual(lastConfirmationPayload, { confirm: true, provider: 'alpaca' });
passed++;
console.log('PASS desktop model configuration preserves id, sends vault secret only to engine, derives model kind, and confirms with provider');
await actions.submitOrder({ symbol: 'BTC/USD', side: 'buy', qty: .01, price: 67000, provider: 'kraken' });
assert.equal(lastProposalPayload.provider, 'kraken');
context.test.setState({ ...context.test.getState(), mode: 'paper' });
await actions.submitOrder({ symbol: 'BTC/USD', side: 'buy', qty: .01, price: 67000, provider: 'alpaca' });
assert(!('provider' in lastPaperPayload));
passed++;
console.log('PASS live proposals include selected provider while paper payloads omit unsupported fields');
const instruments = await context.test.getInstruments('alpaca');
assert.equal(instruments.length, 2);
assert.equal(instruments[0].category, 'crypto');
assert.equal(instruments[1].category, 'stock');
assert(instruments.every(m => m.price === 0 && m.change === 0 && m.probability === 0 && m.volume === '—'));
assert.equal((await context.test.getInstruments('fixture')).length, data.markets.length);
context.test.setConnection(null);
passed++;
console.log('PASS instrument discovery normalizes asset descriptors without leaking fixture prices');
console.log(`Frontend checks: ${passed} passed`);
