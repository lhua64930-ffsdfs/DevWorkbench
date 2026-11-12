"""Exercise encrypted backup, authenticated restore and overwrite refusal on a validation DB."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-isolated-database", action="store_true")
    parser.add_argument("--pg-bin")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    url = make_url(os.environ["DATABASE_URL"])
    if not args.allow_isolated_database or not str(url.database).endswith("_validation") or not url.drivername.startswith("postgresql"):
        parser.error("Use an explicitly acknowledged PostgreSQL *_validation database")
    stage = Path(args.output).resolve().parent
    stage.mkdir(parents=True, exist_ok=True)
    label = uuid4().hex[:12]
    uploads, restored = stage / f"backup-uploads-{label}", stage / f"restored-uploads-{label}"
    uploads.mkdir()
    (uploads / "synthetic.txt").write_text("CodePilot recovery acceptance fixture.\n", encoding="utf-8")
    archive = stage / f"recovery-{label}.cpbackup"
    target_name = f"codepilot_restore_{label}_validation"
    target_url = url.set(database=target_name)
    engine = create_engine(url)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        # Identifier contains only our fixed prefix and random hexadecimal suffix.
        db.execute(text(f'CREATE DATABASE "{target_name}"'))
    env = os.environ.copy()
    env["BACKUP_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
    env["RESTORE_DATABASE_URL"] = target_url.render_as_string(hide_password=False)
    command = [sys.executable, str(ROOT / "scripts/backup_restore.py")]
    if args.pg_bin:
        command += ["--pg-bin", args.pg_bin]
    def execute(action, file, destination, extra=(), expect=0):
        result = subprocess.run([*command, action, "--archive", str(file), "--uploads", str(destination), *extra], env=env, capture_output=True, text=True)
        assert result.returncode == expect, f"{action} returned unexpected status; inspect local configuration"
    started = time.monotonic()
    execute("create", archive, uploads, ["--quiesced"])
    backup_seconds = time.monotonic() - started
    data = bytearray(archive.read_bytes())
    data[40] ^= 1
    damaged = stage / f"tampered-{label}.cpbackup"
    damaged.write_bytes(data)
    execute("restore", damaged, restored, expect=1)
    target = create_engine(target_url)
    assert inspect(target).get_table_names() == []
    started = time.monotonic()
    execute("restore", archive, restored)
    restore_seconds = time.monotonic() - started
    tables = inspect(engine).get_table_names()
    with engine.connect() as source, target.connect() as destination:
        for name in tables:
            statement = text('SELECT count(*) FROM "' + name.replace('"', '""') + '"')
            assert source.scalar(statement) == destination.scalar(statement), f"Row count differs: {name}"
    assert (uploads / "synthetic.txt").read_bytes() == (restored / "synthetic.txt").read_bytes()
    execute("restore", archive, restored, expect=1)
    report = {"passed": True, "encrypted_archive": True, "tamper_rejected_before_db_write": True, "tables_compared": len(tables), "upload_sha256": hashlib.sha256((restored / "synthetic.txt").read_bytes()).hexdigest(), "existing_target_overwrite_refused": True, "backup_seconds": round(backup_seconds, 2), "restore_seconds": round(restore_seconds, 2), "scope": "Small synthetic acceptance fixture; durations are not a production RPO/RTO promise"}
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
