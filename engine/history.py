"""Optional local DuckDB/Parquet history store; imported only when used."""
from pathlib import Path


class HistoryStore:
    def __init__(self, directory):
        import duckdb
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.directory / "history.duckdb"))
        # Atomic migration keeps every legacy row; provider is part of the new key.
        exists = self.db.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='candles'").fetchone()[0]
        columns = [r[1] for r in self.db.execute("PRAGMA table_info('candles')").fetchall()] if exists else []
        self.db.execute("BEGIN TRANSACTION")
        try:
            if exists and "provider" not in columns:
                self.db.execute("ALTER TABLE candles RENAME TO candles_legacy")
            self.db.execute("CREATE TABLE IF NOT EXISTS candles(provider VARCHAR, symbol VARCHAR, interval VARCHAR, time BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, source VARCHAR, PRIMARY KEY(provider,symbol,interval,time))")
            if exists and "provider" not in columns:
                self.db.execute("INSERT INTO candles SELECT coalesce(source,'legacy'),* FROM candles_legacy")
                self.db.execute("DROP TABLE candles_legacy")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def put(self, symbol, interval, candles, provider=None):
        rows = [(provider or c.get("source","legacy"), symbol, interval, int(c["time"]), *[float(c[f]) for f in ("open","high","low","close","volume")], provider or c.get("source","legacy")) for c in candles]
        if not rows:
            return
        self.db.execute("BEGIN TRANSACTION")
        try:
            self.db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",rows)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def read(self, symbol, interval, start=0, end=2**63-1, provider=None):
        if provider is None:
            sources = self.db.execute("SELECT DISTINCT provider FROM candles WHERE symbol=? AND interval=?",[symbol,interval]).fetchall()
            if len(sources)>1:
                raise ValueError("Select a provider; cached histories cannot be mixed")
        query="SELECT time,open,high,low,close,volume,source FROM candles WHERE symbol=? AND interval=? AND time BETWEEN ? AND ?"
        params=[symbol,interval,start,end]
        if provider is not None:
            query+=" AND provider=?"
            params.append(provider)
        cursor=self.db.execute(query+" ORDER BY time",params)
        names=[x[0] for x in cursor.description]
        return [dict(zip(names,row)) for row in cursor.fetchall()]

    def export_parquet(self, path):
        # DuckDB's filename parameter avoids concatenating untrusted SQL paths.
        self.db.execute("COPY candles TO ? (FORMAT PARQUET)", [str(Path(path).resolve())])

    def close(self):
        self.db.close()
