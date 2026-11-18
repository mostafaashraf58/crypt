"""Independent mode and real-data contracts; no real exchange orders are sent."""
import asyncio
from copy import deepcopy
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from engine.api import create_app
from engine.core import Engine
from engine.models import ModelRegistry, mock_prediction
from engine.providers import BitfinexProvider, ProviderError, Quote, provider_factory

TOKEN = 'isolated-workspaces-' + 'x' * 48
AUTH = {'Authorization':'Bearer ' + TOKEN}


def bars(interval=60, count=40):
    end=int(time.time()//interval)*interval
    return [{'time':end-(count-1-i)*interval,'open':100+i,'close':101+i,'high':102+i,'low':99+i,'volume':i+1} for i in range(count)]


class PublicMarket:
    def __init__(self):self.sent=[]
    async def account(self):return {'provider':'bitfinex','account_id':'test-account','currency':'USDT','cash':100000,'equity':100000,'positions':[],'orders':[],'synced_at':time.time()}
    async def candles(self,symbol,interval='1m',limit=200):return bars(60 if interval=='1m' else 3600)
    async def quote(self,symbol):return Quote(symbol,100,time.time(),'bitfinex')
    async def close(self):pass
    async def stream(self,symbols):
        await asyncio.Event().wait()
        if False:yield None


class IsolatedWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_autonomy_survives_viewing_and_configuring_paper(self):
        with tempfile.TemporaryDirectory() as temp:
            # Long scheduler interval keeps this authority test free of decisions.
            app=create_app(temp,TOKEN,production=True,tick_seconds=100)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver',headers=AUTH) as client:
                    with patch('engine.api.provider_factory',return_value=PublicMarket()),patch('engine.api.secure_credentials'):
                        self.assertEqual((await client.post('/connections',json={'provider':'bitfinex','key':'unused','secret':'unused','paper':False})).status_code,200)
                    active=await client.post('/engine/start',json={'source':'bitfinex','execution':'autonomous','confirm_autonomous':True,'max_order_notional':100})
                    self.assertEqual(active.status_code,200,active.text)
                    await client.get('/workspaces/paper/snapshot')
                    self.assertEqual((await client.put('/workspaces/paper/strategies/select',json={'symbols':[]})).status_code,200)
                    live=(await client.get('/snapshot')).json()
                    self.assertEqual(live['execution']['mode'],'autonomous')
                    self.assertTrue(live['running'])
                    self.assertEqual(live['proposals'],[])
                    await client.post('/engine/pause')
                    self.assertEqual((await client.get('/snapshot')).json()['execution']['mode'],'manual')

    async def test_paper_uses_public_quotes_and_does_not_touch_live_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            app=create_app(temp,TOKEN,production=True,tick_seconds=.02)
            market=PublicMarket()
            async with app.router.lifespan_context(app):
                paper=app.state.workspaces['paper'].state.core
                live=app.state.core
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver',headers=AUTH) as client:
                    with patch('engine.api.provider_factory',return_value=market),patch('engine.api.secure_credentials'):
                        self.assertEqual((await client.post('/connections',json={'provider':'bitfinex','key':'unused','secret':'unused'})).status_code,200)
                    # Simulate an active live session with its strategies disabled.
                    for strategy in live.state['strategies'].values():strategy['enabled']=False
                    live.set_running(True)
                    self.assertEqual((await client.get('/workspaces/paper/snapshot')).json()['mode'],'paper')
                    self.assertTrue(live.state['running'])
                    with patch('engine.api.provider_factory',return_value=market):
                        result=await client.post('/workspaces/paper/orders',json={'symbol':'BTC/USDT','side':'buy','qty':1,'price':99999,'id':'paper-only'})
                        self.assertEqual(result.status_code,200,result.text)
                        self.assertAlmostEqual(result.json()['price'],100.05)
                        started=await client.post('/workspaces/paper/engine/start',json={'source':'bitfinex'})
                        self.assertEqual(started.status_code,200,started.text)
                    await asyncio.sleep(.07)
                    self.assertTrue(paper.state['running'])
                    self.assertTrue(live.state['running'])
                    self.assertEqual(live.state['orders'],[])
                    self.assertEqual(live.state['proposals'],[])
                    # Reading live and changing its selection cannot pause the paper runtime.
                    await client.get('/snapshot')
                    selected=await client.put('/strategies/select',json={'symbols':[]})
                    self.assertEqual(selected.status_code,200,selected.text)
                    self.assertTrue(paper.state['running'])
                    self.assertFalse(live.state['running'])
                    self.assertEqual((await client.post('/workspaces/paper/connections',json={'provider':'bitfinex','key':'unused','secret':'unused'})).status_code,400)
                    self.assertEqual((await client.post('/workspaces/paper/mode',json={'mode':'live'})).status_code,400)
                    self.assertEqual((await client.get('/workspaces/status',headers={'Authorization':'bad'})).status_code,401)
                    self.assertTrue((await client.get('/workspaces/status')).json()['paper']['running'])
            reopened=create_app(temp,TOKEN,production=True)
            async with reopened.router.lifespan_context(reopened):
                persisted=reopened.state.workspaces['paper'].state.core
                self.assertTrue(any(o['id']=='paper-only' for o in persisted.state['orders']))
                self.assertFalse(persisted.state['running'])
                self.assertFalse(reopened.state.core.state['running'])

    async def test_model_prediction_ignores_forming_candle_and_logs_input(self):
        with tempfile.TemporaryDirectory() as temp:
            app=create_app(temp,TOKEN,production=True)
            market=PublicMarket()
            observations=bars()
            observations[-1]['close']=1000000  # Still forming: must never reach model.
            observations[-1]['high']=1000001
            market.candles=AsyncMock(return_value=observations)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver',headers=AUTH) as client:
                    with patch('engine.api.provider_factory',return_value=market):
                        r=await client.post('/models/builtin-momentum/predict',json={'symbol':'BTC/USDT','interval':'1m','source':'bitfinex'})
                    self.assertEqual(r.status_code,200,r.text)
                    result=r.json()
                    self.assertEqual(result['probability_up'],mock_prediction(observations[:-1]))
                    self.assertFalse(result['calibrated'])
                    self.assertEqual(result['last_close'],observations[-2]['close'])
                    self.assertEqual(result['target_time'],observations[-1]['time']+60)
                    self.assertEqual(len(app.state.registry.history()),1)
                    for row in observations:row['time']-=86400
                    stale=await client.post('/models/builtin-momentum/predict',json={'symbol':'BTC/USDT','interval':'1m','source':'bitfinex'})
                    self.assertEqual(stale.status_code,400,stale.text)
                    self.assertEqual(len(app.state.registry.history()),1)

    async def test_incomplete_account_hides_old_totals_and_retains_raw_records(self):
        class AccountMarket(PublicMarket):
            incomplete=False
            async def account(self):
                return {'provider':'bitfinex','account_id':'real-account-id','currency':'USDT','cash':1000,'equity':None if self.incomplete else 1000,'positions':[],
                        'orders':[{'id':'external-order','symbol':'BTC/USDT','qty':1,'filled_qty':0,'status':'open'}],
                        'wallets':[{'type':'funding','currency':'BTC','balance':2,'available':1,'unsettled_interest':0}],
                        'reconciliation_complete':not self.incomplete,'reconciliation_errors':['Unknown basis'] if self.incomplete else [],'synced_at':time.time()}
            async def order_history(self,**kwargs):return {'orders':[{'id':'external-closed'}],'next_end':None,'complete':True}
        with tempfile.TemporaryDirectory() as temp:
            app=create_app(temp,TOKEN,production=True)
            market=AccountMarket()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver',headers=AUTH) as client:
                    with patch('engine.api.provider_factory',return_value=market),patch('engine.api.secure_credentials'):
                        connected=await client.post('/connections',json={'provider':'bitfinex','key':'unused','secret':'unused','paper':False})
                    self.assertEqual(connected.status_code,200,connected.text)
                    self.assertTrue((await client.get('/snapshot')).json()['account_ready'])
                    self.assertEqual((await client.get('/exchange/orders')).json()['orders'][0]['id'],'external-closed')
                    market.incomplete=True
                    self.assertEqual((await client.post('/connections/bitfinex/sync')).status_code,400)
                    state=(await client.get('/snapshot')).json()
                    self.assertFalse(state['account_ready'])
                    self.assertIsNone(state['equity'])
                    self.assertIsNone(state['cash'])
                    self.assertEqual(state['positions'],[])
                    self.assertEqual(state['broker_orders'][0]['id'],'external-order')
                    self.assertEqual(state['connections'][0]['account']['wallets'][0]['balance'],2)


class CandleAndHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_weekly_native_timestamp_and_forming_values_are_preserved(self):
        adapter=BitfinexProvider()
        self.addAsyncCleanup(adapter.close)
        monday=1704067200000
        adapter._public=AsyncMock(return_value=[[monday,10,11,12,9,2]])
        result=await adapter.candles('BTC/USDT','1w',2)
        self.assertEqual(result,[{'time':monday//1000,'open':10.,'close':11.,'high':12.,'low':9.,'volume':2.}])
        forming=int(time.time()//60)*60000
        adapter._public=AsyncMock(return_value=[[forming,10,11,12,9,2]])
        self.assertEqual((await adapter.candles('BTC/USDT','1m',2))[0]['time'],forming//1000)

    async def test_order_pages_use_creation_cursor_and_preserve_raw_fields(self):
        adapter=BitfinexProvider({'key':'unused','secret':'unused'},paper=False)
        self.addAsyncCleanup(adapter.close)
        def order(ident,created,updated):return [ident,None,ident,'tBTCUST',created,updated,.4,1,'EXCHANGE LIMIT',None,None,None,0,'CANCELED',None,None,100,101]
        adapter._private=AsyncMock(return_value=[order(1,3000,9999),order(2,2000,8888)])
        page=await adapter.order_history(limit=2)
        self.assertEqual(page['next_end'],2000)
        self.assertEqual(page['orders'][0]['filled_qty'],.6)
        self.assertEqual(page['orders'][0]['raw'][5],9999)
        adapter._private=AsyncMock(return_value=[order(2,2000,8888),order(3,1000,7777)])
        self.assertEqual((await adapter.order_history(end=2000,limit=2))['next_end'],1000)
        adapter._private=AsyncMock(return_value=[order(2,2000,8888),order(3,2000,7777)])
        with self.assertRaisesRegex(ProviderError,'saturated'):await adapter.order_history(end=2000,limit=2)


class ProfitTargetTests(unittest.TestCase):
    def test_profit_target_sells_and_persists_independently_of_model(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=Engine(temp)
            try:
                engine.configure_strategy({'symbol':'BTC/USDT','take_profit':.05,'enabled':False})
                engine.paper_order('BTC/USDT','BUY',1,100)
                engine.set_running(True)
                signal=engine.tick('BTC/USDT',110,None,completed=False)
                self.assertEqual(signal['action'],'SELL')
                self.assertEqual(signal['reason'],'Take-profit target triggered')
                self.assertGreater(engine.snapshot()['realized_pnl'],0)
            finally:engine.close()
            engine=Engine(temp)
            try:self.assertEqual(engine.state['strategies']['BTC/USDT']['take_profit'],.05)
            finally:engine.close()
