import tempfile
import unittest
from pathlib import Path

try:
    import duckdb
except ImportError:
    duckdb = None

from engine.history import HistoryStore
from engine.fixtures import generate_candles


@unittest.skipIf(duckdb is None, "Install project requirements to test DuckDB/Parquet")
class HistoryTests(unittest.TestCase):
    def test_historical_upsert_survives_reopen_and_exports_parquet(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(directory)
            candles = generate_candles("BTC/USD", count=20)
            store.put("BTC/USD", "1h", candles)
            store.put("BTC/USD", "1h", candles)
            self.assertEqual(len(store.read("BTC/USD", "1h")), 20)
            self.assertEqual(len(store.read("BTC/USD", "1h", candles[5]["time"], candles[9]["time"])), 5)
            output = Path(directory) / "history.parquet"
            store.export_parquet(output)
            exported = duckdb.connect()
            self.assertEqual(exported.execute("SELECT count(*) FROM read_parquet(?)", [str(output)]).fetchone()[0], 20)
            exported.close()
            store.close()
            store = HistoryStore(directory)
            rows = store.read("BTC/USD", "1h")
            self.assertEqual(rows[0]["close"], candles[0]["close"])
            self.assertEqual(rows[-1]["source"], "fixture")
            store.close()
