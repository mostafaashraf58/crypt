export type Mode = 'paper' | 'live';
export type Side = 'buy' | 'sell';
export interface Market { symbol: string; name: string; price: number; change: number; volume: string; category: 'crypto' | 'stock'; color: string; icon: string; probability: number }
export interface Candle { time: number; open: number; high: number; low: number; close: number; volume: number }
export interface Position { symbol: string; qty: number; avgPrice: number; price: number; value: number; pnl: number; pnlPercent: number; allocation: number; peak: number }
export interface Order { id: string; symbol: string; side: Side; qty: number; price: number; total: number; fee: number; status: 'filled' | 'pending_review' | 'cancelled' | 'submitted'; time: string; mode: Mode; reason?: string; source?: string }
export interface Signal { id: string; symbol: string; action: 'BUY' | 'SELL' | 'HOLD' | 'WAIT'; probability: number; threshold: number; time: string; model: string; reason: string }
export interface Strategy { id?: string; symbol: string; interval: string; threshold: number; exitThreshold: number; fixedStop: number; trailingStop: number; allocation: number; modelId: string; enabled: boolean }
export interface Model { id: string; name: string; endpoint: string; version: string; timeout: number; status: 'healthy' | 'offline' | 'unknown'; kind: 'mock' | 'http'; latency?: number; assets?: number }
export interface Activity { id: string; time: string; title: string; detail: string; type: 'trade' | 'signal' | 'system' | 'warning' }
export interface Risk { maxAllocation: number; maxDrawdown: number; maxExposure: number }
export interface Connection { provider: string; status: string; paper?: boolean; account?: unknown; error?: string }
export interface WorkstationState { cash: number; equity: number; initialCash: number; pnl: number; positions: Position[]; orders: Order[]; signals: Signal[]; strategies: Strategy[]; models: Model[]; running: boolean; mode: Mode; risk: Risk; activity: Activity[]; quotes: Record<string, number>; connections: Connection[]; fixtureStep: number; highWater: number; dataSource: string }
export interface OrderInput { symbol: string; side: Side; qty: number; price: number }
export interface BacktestInput { symbol: string; interval: string; threshold: number; exitThreshold?: number; initialCash?: number; allocation?: number; fixedStop?: number; trailingStop?: number; start?: string; end?: string }
export interface BacktestResult { equity: { time: number; value: number }[]; drawdowns: { time: number; value: number }[]; trades: Order[]; totalReturn: number; maxDrawdown: number; winRate: number; sharpe: number; finalEquity: number; initialCash: number; tradeCount: number; source: string }
export interface ModelHealth { ok: boolean; latency: number; message: string }
