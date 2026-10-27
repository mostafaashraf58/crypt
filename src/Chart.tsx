import {useEffect,useRef} from 'react';
import {createChart, CandlestickSeries, HistogramSeries, LineSeries, ColorType, CrosshairMode, type UTCTimestamp} from 'lightweight-charts';
import {useMarketCandles} from './api';

export function TradingChart({symbol,interval='1h',area=false,indicator=false,light=false,source}:{symbol:string;interval?:string;area?:boolean;indicator?:boolean;light?:boolean;source?:string}){
 const ref=useRef<HTMLDivElement>(null);
 const {candles}=useMarketCandles(symbol,interval,source);
 useEffect(()=>{
  if(!ref.current) return;
  const chart=createChart(ref.current,{autoSize:true,layout:{background:{type:ColorType.Solid,color:'transparent'},textColor:light?'#738078':'#77808c',fontFamily:'DM Sans, sans-serif',fontSize:10,attributionLogo:false},grid:{vertLines:{color:light?'#e9ece9':'#22272f'},horzLines:{color:light?'#e9ece9':'#22272f'}},rightPriceScale:{borderVisible:false,scaleMargins:{top:.12,bottom:.23}},timeScale:{borderVisible:false,timeVisible:true,secondsVisible:false},crosshair:{mode:CrosshairMode.Normal,vertLine:{color:'#76847d',labelBackgroundColor:'#35443b'},horzLine:{color:'#76847d',labelBackgroundColor:'#35443b'}},handleScroll:true,handleScale:true});

  if(area){const s=chart.addSeries(LineSeries,{color:'#addeb6',lineWidth:2,priceLineVisible:true});s.setData(candles.map(c=>({time:c.time as UTCTimestamp,value:c.close})));}
  else {const s=chart.addSeries(CandlestickSeries,{upColor:'#9bd7b1',downColor:'#d47e86',borderVisible:false,wickUpColor:'#9bd7b1',wickDownColor:'#d47e86',priceLineColor:'#9bd7b1',priceLineStyle:2});s.setData(candles.map(c=>({...c,time:c.time as UTCTimestamp})));}
  const v=chart.addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:'volume',lastValueVisible:false,priceLineVisible:false});
  v.priceScale().applyOptions({scaleMargins:{top:.85,bottom:0}});
  v.setData(candles.map(c=>({time:c.time as UTCTimestamp,value:c.volume,color:c.close>=c.open?'#9bd7b133':'#d47e8633'})));
  if(indicator){const s=chart.addSeries(LineSeries,{color:'#c7b779',lineWidth:1,lastValueVisible:false,priceLineVisible:false});s.setData(candles.slice(19).map((c,i)=>({time:c.time as UTCTimestamp,value:candles.slice(i,i+20).reduce((a,b)=>a+b.close,0)/20})));}
  chart.timeScale().fitContent();return()=>chart.remove();
 },[symbol,interval,area,indicator,light,candles]);
 return <div className="trading-chart" ref={ref} aria-label={`${symbol} ${interval} simulated candlestick chart`}/>;
}

export function Sparkline({values,negative=false,width=96,height=34,fill=false}:{values?:number[];negative?:boolean;width?:number;height?:number;fill?:boolean}){
 const data=values||[20,22,19,27,24,26,24,32,30,38,35,39,37,44];const lo=Math.min(...data),hi=Math.max(...data);const pts=data.map((v,i)=>`${i/(data.length-1)*width},${height-4-(v-lo)/(hi-lo||1)*(height-8)}`).join(' ');
 return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={negative?'spark negative':'spark'} aria-hidden="true">{fill&&<polygon points={`0,${height} ${pts} ${width},${height}`} fill="currentColor" opacity=".07"/>}<polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"/></svg>;
}
