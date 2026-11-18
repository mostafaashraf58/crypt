import {useEffect,useRef,useState} from 'react';
import {createChart, CandlestickSeries, HistogramSeries, LineSeries, ColorType, CrosshairMode, type UTCTimestamp} from 'lightweight-charts';
import {useMarketCandles} from './api';
import type {Candle} from './types';

export function TradingChart({symbol,interval='1h',area=false,indicator=false,light=false,source}:{symbol:string;interval?:string;area?:boolean;indicator?:boolean;light?:boolean;source?:string}){
 const ref=useRef<HTMLDivElement>(null);
 const {candles}=useMarketCandles(symbol,interval,source);
 const [selected,setSelected]=useState<Candle|null>(null);
 const candleRef=useRef(candles);candleRef.current=candles;
 const series=useRef<{chart:ReturnType<typeof createChart>;price:any;volume:any;sma:any;fitted:boolean}|null>(null);
 useEffect(()=>{
  if(!ref.current)return;
  const chart=createChart(ref.current,{autoSize:true,layout:{background:{type:ColorType.Solid,color:'transparent'},textColor:light?'#738078':'#77808c',fontFamily:'DM Sans, sans-serif',fontSize:10,attributionLogo:false},grid:{vertLines:{color:light?'#e9ece9':'#22272f'},horzLines:{color:light?'#e9ece9':'#22272f'}},rightPriceScale:{borderVisible:false,scaleMargins:{top:.12,bottom:.23}},timeScale:{borderVisible:false,timeVisible:true},crosshair:{mode:CrosshairMode.Normal}});
  const price=area?chart.addSeries(LineSeries,{color:'#addeb6',lineWidth:2}):chart.addSeries(CandlestickSeries,{upColor:'#9bd7b1',downColor:'#d47e86',borderVisible:false,wickUpColor:'#9bd7b1',wickDownColor:'#d47e86'});
  const volume=chart.addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:'volume',lastValueVisible:false,priceLineVisible:false});
  volume.priceScale().applyOptions({scaleMargins:{top:.85,bottom:0}});
  const sma=indicator?chart.addSeries(LineSeries,{color:'#c7b779',lineWidth:1,lastValueVisible:false,priceLineVisible:false}):null;
  series.current={chart,price,volume,sma,fitted:false};
  setSelected(null);
  chart.subscribeClick(event=>{if(event.time!=null){const candle=candleRef.current.find(c=>c.time===Number(event.time));if(candle)setSelected(candle);}});
  return()=>{series.current=null;chart.remove();};
 },[symbol,interval,area,indicator,light,source]);
 useEffect(()=>{
  const view=series.current;if(!view)return;
  if(!candles.length){view.price.setData([]);view.volume.setData([]);view.sma?.setData([]);setSelected(null);return;}
  const minimum=Math.min(...candles.map(c=>c.low));
  const precision=Math.max(2,Math.min(12,7-Math.floor(Math.log10(minimum))));
  view.price.applyOptions({priceFormat:{type:'price',precision,minMove:10**-precision}});
  setSelected(previous=>previous?candles.find(c=>c.time===previous.time)??null:null);
  view.price.setData(area?candles.map(c=>({time:c.time as UTCTimestamp,value:c.close})):candles.map(c=>({...c,time:c.time as UTCTimestamp})));
  view.volume.setData(candles.map(c=>({time:c.time as UTCTimestamp,value:c.volume,color:c.close>=c.open?'#9bd7b133':'#d47e8633'})));
  view.sma?.setData(candles.slice(19).map((c,i)=>({time:c.time as UTCTimestamp,value:candles.slice(i,i+20).reduce((total,bar)=>total+bar.close,0)/20})));
  if(!view.fitted){view.chart.timeScale().fitContent();view.fitted=true;}
 },[candles,symbol,interval,area,indicator,light,source]);
 return <><div ref={ref} style={{height:300,width:'100%'}} aria-label={`${symbol} ${interval} price chart`}/><div className="candle-inspector" aria-live="polite">{selected?<><strong>{new Date(selected.time*1000).toISOString().replace('T',' ').replace('.000Z',' UTC')}</strong><span>{symbol} · {interval} · {(source??'fixture').toUpperCase()}</span><span>Open {selected.open} · High {selected.high} · Low {selected.low} · Close {selected.close}</span><span>Volume {selected.volume} {symbol.split('/')[0]} · {selected.time+({'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400,'1d':86400,'1w':604800}[interval]??3600)>Date.now()/1000?'Forming':'Completed'}</span><button className="text-link" onClick={()=>setSelected(null)}>Clear selection</button></>:<span>Click a candle to inspect its exact timestamp, OHLC and base volume.</span>}</div></>;
}

export function Sparkline({values,negative=false,width=96,height=34,fill=false}:{values?:number[];negative?:boolean;width?:number;height?:number;fill?:boolean}){
 const data=values||[20,22,19,27,24,26,24,32,30,38,35,39,37,44];const lo=Math.min(...data),hi=Math.max(...data);const pts=data.map((v,i)=>`${i/(data.length-1)*width},${height-4-(v-lo)/(hi-lo||1)*(height-8)}`).join(' ');
 return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={negative?'spark negative':'spark'} aria-hidden="true">{fill&&<polygon points={`0,${height} ${pts} ${width},${height}`} fill="currentColor" opacity=".07"/>}<polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"/></svg>;
}
