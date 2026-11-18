"""Durable, resumable historical model replay; never submits exchange orders."""
import asyncio
import time
from uuid import uuid4
from .backtest import run_backtest


class BacktestJobs:
    def __init__(self, store, registry):
        self.store,self.registry=store,registry
        self.tasks={}

    def get(self, ident):
        record=self.store.record("backtests",ident)
        if record is None:
            raise ValueError("Backtest job not found")
        if record["status"]=="running" and ident not in self.tasks:
            record["status"]="interrupted"
        return record

    def create(self, config, rows, model_id, source):
        ident=str(uuid4())
        record={"id":ident,"config":config,"rows":rows,"model_id":model_id,"source":source,
                "probabilities":[],"status":"queued","progress":0,"created":time.time(),"model_version":None}
        self.store.record("backtests",ident,record,write=True)
        self.start(ident)
        return self.public(ident)

    def public(self, ident):
        return {k:v for k,v in self.get(ident).items() if k not in {"rows","probabilities"}}

    def start(self, ident):
        if ident in self.tasks:
            return
        record=self.get(ident)
        if record["status"]=="completed":
            return
        async def run():
            try:
                record["status"]="running"
                rows=record["rows"]
                for i in range(len(record["probabilities"]),len(rows)):
                    if i<20:
                        q=.5
                    else:
                        result=await self.registry.predict(record["model_id"],record["config"]["symbol"],record["config"]["interval"],rows[max(0,i-199):i+1])
                        version=result["model_version"]
                        if record["model_version"] is not None and version!=record["model_version"]:
                            raise ValueError("Model version changed during replay; use a pinned version")
                        record["model_version"]=version
                        q=result["probability_up"]
                    record["probabilities"].append(q)
                    record["progress"]=(i+1)/len(rows)
                    self.store.record("backtests",ident,record,write=True)
                    await asyncio.sleep(0)
                result=run_backtest(**record["config"],candles=rows,probabilities=record["probabilities"])
                result.update(source=record["source"],model=record["model_id"],model_version=record["model_version"],lookahead=False)
                record.update(status="completed",result=result)
            except asyncio.CancelledError:
                record["status"]="interrupted"
                raise
            except Exception as exc:
                record.update(status="failed",error=str(exc) if isinstance(exc,ValueError) else "Model replay failed")
            finally:
                try:
                    self.store.record("backtests",ident,record,write=True)
                finally:
                    self.tasks.pop(ident,None)
        self.tasks[ident]=asyncio.create_task(run())

    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
