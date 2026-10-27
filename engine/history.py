"""Optional local DuckDB/Parquet history store; imported only when used."""
from pathlib import Path


class HistoryStore:
    def __init__(self, directory):
        import duckdb
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.directory / "history.duckdb"))
        self.db.execute("CREATE TABLE IF NOT EXISTS candles(symbol VARCHAR, interval VARCHAR, time BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, source VARCHAR, PRIMARY KEY(symbol, interval, time))")

    def put(self, symbol, interval, candles):
        rows = [(symbol, interval, int(c["time"]), *[float(c[f]) for f in ("open", "high", "low", "close", "volume")], c.get("source", "provider")) for c in candles]
        self.db.execute("BEGIN TRANSACTION")
        try:
            self.db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def read(self, symbol, interval, start=0, end=2**63 - 1):
        cursor = self.db.execute("SELECT time,open,high,low,close,volume,source FROM candles WHERE symbol=? AND interval=? AND time BETWEEN ? AND ? ORDER BY time", [symbol, interval, start, end])
        names = [x[0] for x in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def export_parquet(self, path):
        # DuckDB's filename parameter avoids concatenating untrusted SQL paths.
        self.db.execute("COPY candles TO ? (FORMAT PARQUET)", [str(Path(path).resolve())])

    def close(self):
        self.db.close()
