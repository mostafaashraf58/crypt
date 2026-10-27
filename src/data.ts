import type { Market, Candle, Strategy, Position, Activity, Model, Signal, WorkstationState } from './types';
export const FIXTURE_TIME = '2026-10-26T14:00:00.000Z';
export const markets: Market[] = [
  {symbol:'BTC/USD',name:'Bitcoin',price:67432.80,change:2.34,volume:'28.42B',category:'crypto',color:'#f7931a',icon:'₿',probability:.74},
  {symbol:'ETH/USD',name:'Ethereum',price:3521.46,change:1.87,volume:'12.85B',category:'crypto',color:'#8b91db',icon:'Ξ',probability:.81},
  {symbol:'SOL/USD',name:'Solana',price:172.84,change:5.21,volume:'3.61B',category:'crypto',color:'#9b80e9',icon:'◎',probability:.86},
  {symbol:'AAPL',name:'Apple Inc.',price:189.98,change:.72,volume:'48.2M',category:'stock',color:'#b5b8c3',icon:'A',probability:.67},
  {symbol:'NVDA',name:'NVIDIA',price:924.79,change:3.12,volume:'54.7M',category:'stock',color:'#83b94a',icon:'N',probability:.77},
  {symbol:'MSFT',name:'Microsoft',price:425.52,change:-.38,volume:'21.6M',category:'stock',color:'#5e9fe2',icon:'M',probability:.48},
  {symbol:'TSLA',name:'Tesla, Inc.',price:175.34,change:-1.24,volume:'79.4M',category:'stock',color:'#e77478',icon:'T',probability:.39},
  {symbol:'SPY',name:'SPDR S&P 500 ETF',price:527.37,change:.86,volume:'63.8M',category:'stock',color:'#638de7',icon:'S',probability:.71},
];
export const intervals: Record<string,number> = {'1m':60,'5m':300,'15m':900,'30m':1800,'1h':3600,'4h':14400,'1d':86400,'1w':604800};
const seedOf=(s:string)=>Array.from(s).reduce((n,c)=>Math.imul(n^c.charCodeAt(0),16777619)>>>0,2166136261);
function rng(seed:number) { let a=seed; return ()=>{ a|=0; a=a+0x6D2B79F5|0; let t=Math.imul(a^a>>>15,1|a); t=t+Math.imul(t^t>>>7,61|t)^t; return ((t^t>>>14)>>>0)/4294967296; }; }
export function makeCandles(symbol:string, interval='1h', count=120): Candle[] {
  const market=markets.find(m=>m.symbol===symbol)??markets[0]; const rand=rng(seedOf(symbol+interval));
  const step=intervals[interval]??3600; const end=Math.floor(Date.parse(FIXTURE_TIME)/1000/step)*step;
  const raw:Candle[]=[]; let price=market.price*.95;
  for(let i=0;i<count;i++) { const open=price; const wave=Math.sin(i*.22)*.0018; const drift=.00055; const close=open*(1+drift+wave+(rand()-.47)*.009); const spread=open*(.001+rand()*.0038); raw.push({time:end-(count-1-i)*step,open,close,high:Math.max(open,close)+spread,low:Math.min(open,close)-spread*(.5+rand()),volume:Math.round((300+rand()*1900)*(market.category==='crypto'?1:100))}); price=close; }
  const scale=market.price/raw[raw.length-1].close; return raw.map(c=>({...c,open:c.open*scale,high:c.high*scale,low:c.low*scale,close:c.close*scale}));
}
export const initialModels:Model[]=[
 {id:'mock-momentum',name:'Momentum v2',endpoint:'mock://momentum',version:'2.1.0',timeout:5,status:'healthy',kind:'mock',latency:12,assets:3},
 {id:'mock-trend',name:'Trend classifier',endpoint:'mock://trend',version:'1.4.2',timeout:5,status:'healthy',kind:'mock',latency:8,assets:2},
 {id:'local-lstm',name:'Local LSTM',endpoint:'http://127.0.0.1:8081/predict',version:'1.0.0',timeout:10,status:'unknown',kind:'http',assets:0},
];
export const initialStrategies:Strategy[]=markets.slice(0,5).map((m,i)=>({id:'strategy-'+i,symbol:m.symbol,interval:i<3?'1h':'1d',threshold:.65,exitThreshold:.65,fixedStop:.03,trailingStop:.05,allocation:i===0?.2:.12,modelId:i<3?'mock-momentum':'mock-trend',enabled:true}));
const position=(symbol:string,qty:number,avgPrice:number):Position=>{const price=markets.find(m=>m.symbol===symbol)!.price;return{symbol,qty,avgPrice,price,value:qty*price,pnl:(price-avgPrice)*qty,pnlPercent:(price/avgPrice-1)*100,allocation:0,peak:price};};
export const initialPositions:Position[]=[position('BTC/USD',.25,61842.30),position('ETH/USD',3.5,3178.42),position('NVDA',12,861.24),position('AAPL',30,176.14)];
export const initialActivity:Activity[]=[
 {id:'a1',time:'2026-10-26T14:00:00Z',title:'Bitcoin signal updated',detail:'74% upward probability · Momentum v2',type:'signal'},
 {id:'a2',time:'2026-10-26T13:59:00Z',title:'Paper order filled',detail:'Bought 1.5 ETH/USD at $3,512.80',type:'trade'},
 {id:'a3',time:'2026-10-26T13:58:00Z',title:'Risk checks passed',detail:'All positions within configured limits',type:'system'},
 {id:'a4',time:'2026-10-26T13:55:00Z',title:'Offline workspace ready',detail:'Deterministic market fixtures loaded',type:'system'},
];
export const initialSignals:Signal[]=markets.slice(0,5).map((m,i)=>({id:'signal-'+i,symbol:m.symbol,action:i===2?'BUY':'HOLD',probability:m.probability,threshold:.65,time:FIXTURE_TIME,model:i<3?'Momentum v2':'Trend classifier',reason:'Deterministic offline fixture'}));
export function createInitialState():WorkstationState {const equity=104382.64;const positions=initialPositions.map(p=>({...p,allocation:p.value/equity}));return{cash:equity-positions.reduce((s,p)=>s+p.value,0),equity,initialCash:100000,pnl:4382.64,positions,orders:[],signals:initialSignals.map(s=>({...s})),strategies:initialStrategies.map(s=>({...s})),models:initialModels.map(m=>({...m})),running:false,mode:'paper',risk:{maxAllocation:.25,maxDrawdown:.15,maxExposure:.8},activity:initialActivity.map(a=>({...a})),quotes:Object.fromEntries(markets.map(m=>[m.symbol,m.price])),connections:[{provider:'Alpaca',status:'disconnected'},{provider:'Kraken',status:'disconnected'}],fixtureStep:0,highWater:equity,dataSource:'fixture'};}
