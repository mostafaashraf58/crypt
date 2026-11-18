"""Seed/check only an explicitly supplied QA workspace, never a default account directory."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from engine.core import Engine
from engine.models import ModelRegistry
from engine.history import HistoryStore


root=Path(sys.argv[2]).resolve()
workspace=(Path(__file__).resolve().parents[1]/"work").resolve()
if not root.is_relative_to(workspace):
    raise SystemExit("QA data must stay inside the project work directory")
core=Engine(root)
models=ModelRegistry(root)
history=HistoryStore(root/"history")
try:
    if sys.argv[1]=="seed":
        core.configure_strategy({"symbol":"BTC/USDT","threshold":.73})
        core.configure_risk({"price_bound":.017})
        core.set_watchlist(["BTC/USDT","ETH/USDT"])
        core.paper_order("BTC/USD","BUY",1,100,"native-persistence-proof")
        models.add({"id":"qa-model","name":"QA local model metadata","endpoint":"http://localhost:9999/predict","approved_version":"qa-v1"})
        history.put("BTC/USDT","1m",[{"time":1,"open":100,"high":100,"low":100,"close":100,"volume":1,"source":"bitfinex"}])
    else:
        # Inspect preserved paper records independently of the desktop live view.
        core.set_mode("paper")
        state=core.snapshot()
        assert not state["running"]
        assert state["global_risk"]["price_bound"]==.017
        assert state["watchlist"]==["BTC/USDT","ETH/USDT"]
        assert any(s["symbol"]=="BTC/USDT" and s["threshold"]==.73 for s in state["strategies"])
        assert state["orders"][0]["id"]=="native-persistence-proof"
        assert state["positions"][0]["qty"]==1
        core.paper_order("BTC/USD","BUY",1,100,"native-persistence-proof")
        assert core.snapshot()["positions"][0]["qty"]==1
        assert models.get("qa-model")["approved_version"]=="qa-v1"
        assert history.read("BTC/USDT","1m",provider="bitfinex")[0]["close"]==100
        assert core.store.db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        from engine.backup import inspect_backup
        backups=list((root/"backups").glob("backup-*.zip"))
        assert backups, "Packaged startup did not create its automatic backup"
        manifest=inspect_backup(max(backups,key=lambda p:p.stat().st_mtime))
        assert set(manifest["files"])=={"axiom.sqlite3","models.sqlite3","history/history.duckdb"}
    print("PASS "+sys.argv[1]+": positions, orders, idempotency, strategies, limits, watchlist, models and provider history")
finally:
    history.close();models.close();core.close()
