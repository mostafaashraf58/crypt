import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
import zipfile

import httpx

from engine.backup import create_backup, inspect_backup, schedule_restore
from engine.core import Engine
from engine.history import HistoryStore
from engine.models import ModelRegistry, ModelError
from engine.providers import BitfinexProvider, OrderNotSubmitted, Quote
import test_autonomous as fixtures


class PersistenceTests(unittest.TestCase):
    def test_newer_schema_is_preserved_and_failed_open_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            db=sqlite3.connect(root/"axiom.sqlite3")
            db.execute("PRAGMA user_version=99");db.close()
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError,"Unsupported database version"):
                    Engine(root)
            db=sqlite3.connect(root/"axiom.sqlite3")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],99)
            db.close()

    def test_committed_orders_survive_abrupt_process_exit(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temp:
            code="from engine.core import Engine; import os,sys; e=Engine(sys.argv[1]); e.paper_order('BTC/USD','BUY',1,100,'crash-safe'); os._exit(0)"
            subprocess.run([sys.executable,"-c",code,temp],check=True)
            core=Engine(temp)
            try:
                self.assertEqual(core.snapshot()["orders"][0]["id"],"crash-safe")
                core.paper_order("BTC/USD","BUY",1,100,"crash-safe")
                self.assertEqual(core.snapshot()["positions"][0]["qty"],1)
                self.assertEqual(core.store.db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            finally:
                core.close()

    def test_paper_reset_preserves_live_and_configuration_data(self):
        with tempfile.TemporaryDirectory() as temp:
            core=Engine(temp)
            try:
                core.set_watchlist(["ETH/USDT"])
                core.remember_connection("bitfinex",{"status":"saved"})
                core.configure_risk({"price_bound":.02})
                core.paper_order("BTC/USD","BUY",1,100)
                result=core.reset()
                self.assertEqual(result["positions"],[])
                self.assertEqual(result["watchlist"],["ETH/USDT"])
                self.assertEqual(result["global_risk"]["price_bound"],.02)
                self.assertIn("bitfinex",result["connections"])
                core.set_running(True)
                core.tick("BTC/USD",100,.99,interval_id=1)
            finally:
                core.close()

    def test_restore_can_recover_a_corrupt_database_without_destroying_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);core=Engine(root);models=ModelRegistry(root)
            core.set_watchlist(["ETH/USDT"])
            backup=create_backup(root,core,models,None)
            schedule_restore(root,backup["name"])
            core.close();models.close()
            (root/"axiom.sqlite3").write_bytes(b"damaged original database")
            core=Engine(root)
            try:
                self.assertEqual(core.state["watchlist"],["ETH/USDT"])
                self.assertEqual((root/"axiom.sqlite3.before-restore").read_bytes(),b"damaged original database")
            finally:
                core.close()

    def test_switching_accounts_does_not_reuse_exit_intents_or_drafts(self):
        with tempfile.TemporaryDirectory() as temp:
            core=Engine(temp)
            try:
                core.set_mode("live")
                core.reconcile_live_account({"provider":"bitfinex","account_id":"A","cash":1000,"equity":1000},[])
                draft=core.propose_order("BTC/USDT","BUY",1,100,source="bitfinex")
                core.state["exit_intents"]["bitfinex:BTC/USDT"]={"provider":"bitfinex","symbol":"BTC/USDT","reason":"old exit"}
                core.reconcile_live_account({"provider":"bitfinex","account_id":"B","cash":1000,"equity":1000},[])
                self.assertEqual(core.state["exit_intents"],{})
                with self.assertRaisesRegex(ValueError,"different account"):
                    core.claim_proposal(draft["id"],100)
            finally:
                core.close()

    def test_saved_state_and_verified_restore_survive_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            engine=Engine(root)
            registry=ModelRegistry(root)
            history=HistoryStore(root/"history")
            engine.configure_strategy({"symbol":"BTC/USDT","threshold":.73})
            engine.configure_risk({"price_bound":.017})
            engine.paper_order("BTC/USD","BUY",1,100,"persisted-order")
            engine.set_watchlist(["BTC/USDT","ETH/USDT"])
            engine.remember_connection("bitfinex",{"paper":False,"status":"saved"})
            engine.store.record("backtests","job",{"status":"interrupted"},write=True)
            registry.add({"id":"real","name":"Real model","endpoint":"http://localhost:8081/predict","approved_version":"v1"})
            candle={"time":1,"open":1,"high":1,"low":1,"close":1,"volume":1,"source":"bitfinex"}
            history.put("BTC/USDT","1m",[candle])
            backup=create_backup(root,engine,registry,history)
            inspect_backup(root/"backups"/backup["name"])
            engine.set_watchlist(["SOL/USDT"])
            schedule_restore(root,backup["name"])
            engine.close();registry.close();history.close()
            engine=Engine(root);registry=ModelRegistry(root);history=HistoryStore(root/"history")
            try:
                state=engine.snapshot()
                self.assertFalse(state["running"])
                self.assertEqual(state["watchlist"],["BTC/USDT","ETH/USDT"])
                self.assertEqual(state["global_risk"]["price_bound"],.017)
                self.assertEqual(next(s for s in state["strategies"] if s["symbol"]=="BTC/USDT")["threshold"],.73)
                self.assertIn("bitfinex",state["connections"])
                self.assertEqual(state["orders"][0]["id"],"persisted-order")
                self.assertEqual(state["positions"][0]["qty"],1)
                engine.paper_order("BTC/USD","BUY",1,100,"persisted-order")
                self.assertEqual(engine.snapshot()["positions"][0]["qty"],1)
                self.assertEqual(engine.store.record("backtests","job")["status"],"interrupted")
                self.assertEqual(registry.get("real")["approved_version"],"v1")
                self.assertEqual(history.read("BTC/USDT","1m",provider="bitfinex")[0]["close"],1)
                self.assertTrue((root/"axiom.sqlite3.before-restore").exists())
            finally:
                engine.close();registry.close();history.close()

    def test_backup_tampering_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);engine=Engine(root);registry=ModelRegistry(root)
            try:
                backup=create_backup(root,engine,registry,None)
                path=root/"backups"/backup["name"]
                with zipfile.ZipFile(path,"a") as archive:
                    archive.writestr("../outside.txt","bad")
                with self.assertRaises(ValueError):
                    inspect_backup(path)
                with self.assertRaises(ValueError):
                    schedule_restore(root,"../outside.zip")
                self.assertFalse((root/"pending-restore.json").exists())
                self.assertEqual(engine.store.db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            finally:
                engine.close();registry.close()

    def test_legacy_history_migrates_and_preserves_provider_isolation(self):
        import duckdb
        with tempfile.TemporaryDirectory() as temp:
            db=duckdb.connect(str(Path(temp)/"history.duckdb"))
            db.execute("CREATE TABLE candles(symbol VARCHAR, interval VARCHAR, time BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, source VARCHAR, PRIMARY KEY(symbol,interval,time))")
            db.execute("INSERT INTO candles VALUES ('BTC/USD','1m',1,100,100,100,100,1,'alpaca')")
            db.close()
            history=HistoryStore(temp)
            try:
                history.put("BTC/USD","1m",[{"time":1,"open":200,"high":200,"low":200,"close":200,"volume":1,"source":"kraken"}])
                self.assertEqual(history.read("BTC/USD","1m",provider="alpaca")[0]["close"],100)
                self.assertEqual(history.read("BTC/USD","1m",provider="kraken")[0]["close"],200)
                with self.assertRaises(ValueError):
                    history.read("BTC/USD","1m")
            finally:
                history.close()

    def test_exit_intent_and_boundary_sizing_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            core=Engine(temp)
            try:
                core.configure_strategy({"symbol":"BTC/USDT","allocation":.25})
                core.set_mode("live")
                core.reconcile_live_account({"provider":"bitfinex","cash":1000,"equity":1000},[])
                core.set_running(True)
                signal=core.tick("BTC/USDT",100,.99,source="bitfinex",interval_id=1,max_order_notional=1000)
                self.assertEqual(signal["action"],"BUY",signal)
                self.assertLessEqual(signal["proposal"]["qty"]*100*1.005,250)
                core.finish_proposal(signal["proposal"]["id"],"filled")
                core.reconcile_live_account({"provider":"bitfinex","cash":0,"equity":1000},[{"symbol":"BTC/USDT","qty":10,"price":100,"entry_price":100}])
                signal=core.tick("BTC/USDT",100,.1,source="bitfinex",interval_id=2,max_order_notional=20)
                core.finish_proposal(signal["proposal"]["id"],"filled")
                core.reconcile_live_account({"provider":"bitfinex","cash":20,"equity":1000},[{"symbol":"BTC/USDT","qty":9.8,"price":100,"entry_price":100}])
                next_tick=core.tick("BTC/USDT",100,None,completed=False,source="bitfinex",max_order_notional=20)
                self.assertEqual(next_tick["action"],"SELL",next_tick)
            finally:
                core.close()
            core=Engine(temp)
            try:
                self.assertIn("bitfinex:BTC/USDT",core.state["exit_intents"])
            finally:
                core.close()


class RobustAutonomyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.AutonomousTests.asyncSetUp
    asyncTearDown=fixtures.AutonomousTests.asyncTearDown
    start=fixtures.AutonomousTests.start
    wait_sent=fixtures.AutonomousTests.wait_sent
    async def test_completed_replays_and_alerts_are_visible_after_reopen(self):
        result=await self.client.post("/backtest",json={"symbol":"BTC/USD","source":"fixture","interval":"1h"})
        self.assertEqual(result.status_code,200,result.text)
        jobs=(await self.client.get("/snapshot")).json()["backtests"]
        self.assertEqual(jobs[0]["status"],"completed")
        self.core.store.record("alerts","qa",{"time":time.time(),"message":"QA operational alert"},write=True)
        await self.life.__aexit__(None,None,None)
        from engine.api import create_app
        self.app=create_app(self.temp.name,"x"*48)
        self.life=self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url="http://testserver",headers={"Authorization":"Bearer "+"x"*48}) as client:
            state=(await client.get("/snapshot")).json()
            self.assertEqual(state["backtests"][0]["id"],jobs[0]["id"])
            self.assertEqual(state["alerts"][0]["message"],"QA operational alert")
            saved=(await client.get("/backtests/"+jobs[0]["id"])).json()
            self.assertEqual(saved["result"]["source"],"fixture")

    async def test_stop_still_executes_when_drawdown_blocks_entries(self):
        self.broker.cash=0;self.broker.qty=10
        self.assertEqual((await self.start()).status_code,200)
        self.broker.price=80
        await self.broker.queue.put(Quote("BTC/USDT",80,time.time(),"bitfinex"))
        await self.wait_sent(1)
        self.assertEqual(self.broker.sent[0]["side"],"SELL")
        self.assertEqual(self.core.snapshot()["risk_status"],"entries_blocked")

    async def test_definitive_preflight_failure_is_not_an_unknown_order(self):
        async def reject(order):
            raise OrderNotSubmitted("Order below exchange minimum")
        self.broker.submit_order=reject
        self.assertEqual((await self.start()).status_code,200)
        for _ in range(100):
            if self.core.snapshot()["proposals"]:
                break
            await asyncio.sleep(.02)
        await asyncio.sleep(.05)
        self.assertEqual(self.core.snapshot()["proposals"][0]["status"],"rejected")

    async def test_saved_connection_and_watchlist_are_restored_without_trading(self):
        await self.client.put("/watchlist",json={"symbols":["ETH/USDT"]})
        await self.life.__aexit__(None,None,None)
        from engine.api import create_app
        self.app=create_app(self.temp.name,"x"*48)
        self.life=self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        state=self.app.state.core.snapshot()
        self.assertEqual(state["watchlist"],["ETH/USDT"])
        self.assertIn("bitfinex",state["connections"])
        self.assertFalse(state["running"])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_snapshot_trade_and_stale_heartbeat(self):
        async with httpx.AsyncClient() as client:
            adapter=BitfinexProvider(client=client)
            list(adapter._parse_stream({"event":"subscribed","chanId":7,"symbol":"tBTCUST"}))
            now=int(time.time()*1000)
            quotes=list(adapter._parse_stream([7,[[1,now,1,100]]]))
            self.assertEqual(quotes[0].symbol,"BTC/USDT")
            self.assertEqual(list(adapter._parse_stream([7,"tu",[1,now,1,100]])),[])
            adapter._latest["BTC/USDT"]=Quote("BTC/USDT",100,time.time()-100,"bitfinex")
            self.assertTrue(list(adapter._parse_stream([7,"hb"]))[0].stale)

    async def test_protective_orders_use_exchange_stop_and_atomic_amendment(self):
        sent=[]
        def handler(request):
            body=json.loads(request.content)
            sent.append((request.url.path,body))
            row=[100,None,body.get("cid",1),"tBTCUST",0,0,float(body["amount"]),float(body["amount"]),"EXCHANGE STOP",None,None,None,0,"ACTIVE",None,None,float(body["price"]),0]
            return httpx.Response(200,json=[0,0,0,0,row if request.url.path.endswith("update") else [row],0,"SUCCESS","ok"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=BitfinexProvider({"key":"fake","secret":"fake"},client=client,paper=False)
            result=await adapter.place_protection("BTC/USDT",1,97,"stop-client",lambda:None)
            self.assertEqual(result["id"],"100")
            self.assertEqual(sent[0][1]["type"],"EXCHANGE STOP")
            self.assertEqual(sent[0][1]["amount"],"-1.00000000")
            await adapter.update_protection("100",.5,98,lambda:None)
            self.assertTrue(sent[1][0].endswith("order/update"))
            self.assertEqual(sent[1][1]["amount"],"-0.50000000")

    async def test_persistent_trade_cache_and_nonce_are_loaded(self):
        with tempfile.TemporaryDirectory() as temp:
            core=Engine(temp)
            async with httpx.AsyncClient() as client:
                adapter=BitfinexProvider({"key":"fake","secret":"fake"},client=client,paper=False)
                adapter.bind_store(core.store)
                adapter._trades={1:[1,"tBTCUST",1000,1,1,100,"EXCHANGE FOK",100,-1,-.1,"UST"]}
                adapter._trade_cursor=1000
                adapter._persist_history()
                core.store.record("bitfinex-nonce",adapter._store_key,9999999999999999,write=True)
                other=BitfinexProvider({"key":"fake","secret":"fake"},client=client,paper=False)
                other.bind_store(core.store)
                self.assertIn(1,other._trades)
                self.assertEqual(other._nonce,9999999999999999)
            core.close()


class ModelVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_version_pin_is_preserved_and_blocks_drift(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            registry=ModelRegistry(Path(temp))
            registry.add({"id":"real","name":"Real","endpoint":"http://localhost:8081/predict","approved_version":"v1"})
            registry.add({"id":"real","name":"Renamed","endpoint":"http://localhost:8081/predict"})
            self.assertEqual(registry.get("real")["approved_version"],"v1")
            original=httpx.AsyncClient
            def handler(request):
                body=json.loads(request.content)
                return httpx.Response(200,json={"request_id":body["request_id"],"model_version":"v2","probability_up":.9})
            with patch("engine.models.httpx.AsyncClient",side_effect=lambda **kwargs:original(transport=httpx.MockTransport(handler))):
                with self.assertRaisesRegex(ModelError,"approved"):
                    await registry.predict("real","BTC/USDT","1m",[])
            registry.close()


class ProtectedBroker(fixtures.Broker):
    def __init__(self):
        super().__init__()
        self.cash=0;self.qty=10
        self.stops={}

    async def account(self):
        account=await super().account()
        account["orders"]=[dict(s) for s in self.stops.values() if s["status"]=="open"]
        for p in account["positions"]:
            p["available_qty"]=max(0,self.qty-sum(s["qty"] for s in account["orders"]))
        return account

    async def place_protection(self,symbol,qty,stop_price,client_id,guard):
        guard()
        ident=str(len(self.stops)+100)
        order={"id":ident,"symbol":symbol,"qty":qty,"filled_qty":0,"status":"open","side":"sell","price":stop_price,"client_order_id":client_id}
        self.stops[ident]=order
        return order.copy()

    async def get_order_by_id(self,ident):
        return self.stops[ident].copy()

    async def cancel_order(self,ident):
        self.stops[ident]["status"]="canceled"
        return {"id":ident,"status":"cancellation_acknowledged"}


class ProtectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.AutonomousTests.asyncSetUp
    asyncTearDown=fixtures.AutonomousTests.asyncTearDown
    start=fixtures.AutonomousTests.start
    wait_sent=fixtures.AutonomousTests.wait_sent

    async def test_exchange_stop_is_released_for_exit_and_recreated_for_remainder(self):
        self.broker=ProtectedBroker()
        self.app.state.adapters["bitfinex"]=self.broker
        response=await self.start()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(self.broker.stops),1)
        self.assertEqual(self.broker.stops["100"]["qty"],10)
        self.broker.price=80
        await self.broker.queue.put(Quote("BTC/USDT",80,time.time(),"bitfinex"))
        await self.wait_sent(1)
        await asyncio.sleep(.05)
        self.assertEqual(self.broker.stops["100"]["status"],"canceled")
        self.assertTrue(any(s["status"]=="open" for s in self.broker.stops.values()))
        self.assertEqual(self.broker.sent[0]["side"],"SELL")
        await self.client.post("/engine/pause")
        self.assertTrue(any(s["status"]=="open" for s in self.broker.stops.values()))


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_model_replay_resumes_from_saved_progress(self):
        from engine.jobs import BacktestJobs
        entered=asyncio.Event()
        class Model:
            def __init__(self,block=False): self.block=block
            async def predict(self,*args):
                if self.block:
                    entered.set()
                    await asyncio.Event().wait()
                await asyncio.sleep(0)
                return {"probability_up":.8,"model_version":"v1"}
        with tempfile.TemporaryDirectory() as temp:
            core=Engine(temp)
            jobs=BacktestJobs(core.store,Model(block=True))
            rows=[{"time":i*60,"open":100,"high":101,"low":99,"close":100,"volume":1} for i in range(60)]
            record=jobs.create({"symbol":"BTC/USDT","interval":"1m"},rows,"real","bitfinex")
            await asyncio.wait_for(entered.wait(),2)
            await jobs.close()
            saved=jobs.get(record["id"])
            self.assertEqual(saved["status"],"interrupted")
            done=len(saved["probabilities"])
            self.assertEqual(done,20)
            core.close()
            core=Engine(temp);jobs=BacktestJobs(core.store,Model())
            try:
                jobs.start(record["id"])
                for _ in range(100):
                    result=jobs.get(record["id"])
                    if result["status"]=="completed":
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(result["status"],"completed",result)
                self.assertEqual(len(result["probabilities"]),60)
                self.assertEqual(result["result"]["source"],"bitfinex")
                self.assertEqual(result["model_version"],"v1")
            finally:
                await jobs.close();core.close()
