"""Verified, secret-free database backups; restore only before databases open."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import zipfile

FILES = ("axiom.sqlite3", "models.sqlite3", "history/history.duckdb")


def inspect_backup(path):
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist())!=len(set(archive.namelist())):
            raise ValueError("Backup contains duplicate entries")
        if set(archive.namelist()) - {*FILES, "manifest.json"}:
            raise ValueError("Backup contains unexpected files")
        if sum(i.file_size for i in archive.infolist()) > 2_000_000_000:
            raise ValueError("Backup is too large")
        manifest=json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != 1 or "axiom.sqlite3" not in manifest.get("files",{}):
            raise ValueError("Unsupported or incomplete backup")
        for name,digest in manifest["files"].items():
            if name not in FILES or hashlib.sha256(archive.read(name)).hexdigest()!=digest:
                raise ValueError("Backup integrity verification failed")
        if set(archive.namelist()) != set(manifest["files"])|{"manifest.json"}:
            raise ValueError("Backup manifest mismatch")
        return manifest


def create_backup(directory, core, registry, history):
    directory=Path(directory)
    folder=directory/"backups"
    folder.mkdir(exist_ok=True)
    name=f"backup-{time.time_ns()}.zip"
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        temp=Path(temp)
        core.store.backup(temp/"axiom.sqlite3")
        db=sqlite3.connect(temp/"models.sqlite3")
        try:
            registry.db.backup(db)
        finally:
            db.close()
        if history:
            import duckdb
            exported=temp/"history-export"
            history.db.execute("EXPORT DATABASE '"+str(exported).replace("'","''")+"' (FORMAT PARQUET)")
            (temp/"history").mkdir()
            copy=duckdb.connect(str(temp/"history/history.duckdb"))
            try:
                copy.execute("IMPORT DATABASE '"+str(exported).replace("'","''")+"'")
            finally:
                copy.close()
        manifest={"schema":1,"created":time.time(),"files":{}}
        target=folder/(name+".tmp")
        with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive:
            for file in FILES:
                if (temp/file).exists():
                    data=(temp/file).read_bytes()
                    manifest["files"][file]=hashlib.sha256(data).hexdigest()
                    archive.writestr(file,data)
            archive.writestr("manifest.json",json.dumps(manifest))
        inspect_backup(target)
        os.replace(target,folder/name)
    return {"name":name,"created":manifest["created"],"path":str(folder/name)}


def schedule_restore(directory, name):
    directory=Path(directory)
    if Path(name).name!=name or not name.endswith(".zip"):
        raise ValueError("Select a backup from this workspace")
    inspect_backup(directory/"backups"/name)
    pending=directory/"pending-restore.json"
    temp=pending.with_suffix(".tmp")
    temp.write_text(json.dumps({"name":name}),encoding="utf-8")
    os.replace(temp,pending)


def apply_pending_restore(directory):
    directory=Path(directory)
    pending=directory/"pending-restore.json"
    if not pending.exists():
        return
    name=json.loads(pending.read_text(encoding="utf-8"))["name"]
    if Path(name).name!=name:
        raise ValueError("Invalid pending restore")
    path=directory/"backups"/name
    inspect_backup(path)
    # Keep original databases for recovery; a pending marker makes interruption retryable.
    with zipfile.ZipFile(path) as archive:
        manifest=json.loads(archive.read("manifest.json"))
        for file in manifest["files"]:
            target=directory/file
            target.parent.mkdir(parents=True,exist_ok=True)
            staged=target.with_suffix(target.suffix+".restore")
            staged.write_bytes(archive.read(file))
            if file.endswith("sqlite3"):
                db=sqlite3.connect(staged)
                try:
                    if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                        raise ValueError("Restored database is corrupt")
                finally:
                    db.close()
            else:
                import duckdb
                db=duckdb.connect(str(staged),read_only=True)
                try:
                    db.execute("SELECT COUNT(*) FROM candles").fetchone()
                finally:
                    db.close()
            before=target.with_suffix(target.suffix+".before-restore")
            if target.exists() and not before.exists():
                if file.endswith("sqlite3"):
                    original=sqlite3.connect(target)
                    recovery=sqlite3.connect(before)
                    damaged=False
                    try:
                        original.backup(recovery)
                    except sqlite3.DatabaseError:
                        damaged=True
                    finally:
                        recovery.close();original.close()
                    if damaged:
                        shutil.copy2(target,before)
                        for suffix in ("-wal","-shm"):
                            sidecar=Path(str(target)+suffix)
                            if sidecar.exists():
                                shutil.copy2(sidecar,Path(str(before)+suffix))
                else:
                    import duckdb
                    try:
                        original=duckdb.connect(str(target))
                        try:
                            original.execute("CHECKPOINT")
                        finally:
                            original.close()
                    except duckdb.Error:
                        wal=Path(str(target)+".wal")
                        if wal.exists():
                            shutil.copy2(wal,Path(str(before)+".wal"))
                    shutil.copy2(target,before)
            os.replace(staged,target)
            for suffix in (("-wal","-shm") if file.endswith("sqlite3") else (".wal",)):
                sidecar=Path(str(target)+suffix)
                if sidecar.exists():
                    sidecar.unlink()
    pending.unlink()
