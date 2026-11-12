"""Authenticated encrypted PostgreSQL + upload backups, restored only to empty targets.

Requires PostgreSQL 18 client tools and cryptography. Credentials are read from
environment, never arguments or logs. Stop API/workers/scheduler before create.
Vector indexes and Git checkouts are rebuildable and intentionally excluded.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import unquote, urlsplit
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"CPBK1\n"
CHUNK = 1024 * 1024


def connection(variable):
    url = urlsplit(os.environ[variable].replace("postgresql+psycopg://", "postgresql://", 1))
    if url.scheme != "postgresql" or not url.hostname or not url.path.strip("/") or url.query or url.fragment:
        raise ValueError("Use a PostgreSQL URL without query parameters")
    env = os.environ.copy()
    env.update(PGHOST=url.hostname, PGPORT=str(url.port or 5432), PGUSER=unquote(url.username or ""), PGPASSWORD=unquote(url.password or ""), PGDATABASE=unquote(url.path[1:]), PGCONNECT_TIMEOUT="10")
    return env


def run_tool(name, args, env, pg_bin=None, capture=False):
    executable = str(Path(pg_bin) / name) if pg_bin else name
    result = subprocess.run([executable, *args], env=env, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        # pg_* diagnostics can contain connection strings or object contents.
        raise RuntimeError(f"{name} failed (exit {result.returncode}); inspect the database service privately")
    return result.stdout.decode() if capture else None


def encryption_key():
    key = base64.urlsafe_b64decode(os.environ["BACKUP_ENCRYPTION_KEY"].encode())
    if len(key) != 32:
        raise ValueError("BACKUP_ENCRYPTION_KEY must encode exactly 32 random bytes")
    return key


def encrypt_file(source, target, key):
    nonce = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(MAGIC)
    try:
        with open(target, "xb") as out, open(source, "rb") as stream:
            os.chmod(target, 0o600)
            out.write(MAGIC + nonce)
            while chunk := stream.read(CHUNK):
                out.write(cipher.update(chunk))
            out.write(cipher.finalize())
            out.write(cipher.tag)
    except Exception:
        # Never remove an archive which existed before this invocation.
        raise


def decrypt_file(source, target, key):
    with open(source, "rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError("Unsupported backup format")
        nonce = stream.read(12)
        stream.seek(-16, 2)
        tag = stream.read(16)
        remaining = stream.tell() - len(MAGIC) - 12 - 16
        if remaining < 0:
            raise ValueError("Truncated backup")
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        cipher.authenticate_additional_data(MAGIC)
        stream.seek(len(MAGIC) + 12)
        with open(target, "xb") as out:
            os.chmod(target, 0o600)
            while remaining:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError("Truncated backup")
                out.write(cipher.update(chunk))
                remaining -= len(chunk)
            out.write(cipher.finalize())  # Authenticate before extracting or touching a DB.


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def create(args):
    if not args.quiesced:
        raise ValueError("Stop all writers and explicitly pass --quiesced")
    uploads = Path(args.uploads).resolve(strict=True)
    output = Path(args.archive).resolve()
    if output.is_relative_to(uploads):
        raise ValueError("Store the backup outside uploads")
    key, env = encryption_key(), connection("DATABASE_URL")
    with tempfile.TemporaryDirectory(prefix="codepilot-backup-") as directory:
        stage = Path(directory)
        run_tool("pg_dump", ["--format=custom", "--no-owner", "--no-acl", "--file", str(stage / "database.dump")], env, args.pg_bin)
        entries = {"database.dump": digest(stage / "database.dump")}
        with tarfile.open(stage / "payload.tar", "w") as archive:
            archive.add(stage / "database.dump", arcname="database.dump", recursive=False)
            for path in sorted(uploads.rglob("*")):
                if path.is_symlink() or not path.resolve().is_relative_to(uploads):
                    raise ValueError("Symlinks are prohibited in upload backups")
                if path.is_file():
                    name = "uploads/" + path.relative_to(uploads).as_posix()
                    entries[name] = digest(path)
                    archive.add(path, arcname=name, recursive=False)
            (stage / "manifest.json").write_text(json.dumps({"format": 1, "files": entries}), encoding="utf-8")
            archive.add(stage / "manifest.json", arcname="manifest.json", recursive=False)
        encrypt_file(stage / "payload.tar", output, key)
    return {"archive": output.name, "sha256": digest(output), "files": len(entries)}


def restore(args):
    source_env, env = connection("DATABASE_URL"), connection("RESTORE_DATABASE_URL")
    identity = lambda value: tuple(value.get(k) for k in ("PGHOST", "PGPORT", "PGDATABASE"))
    if identity(env) == identity(source_env):
        raise ValueError("Restore target must differ from the source database")
    # pg_restore flags also disallow ownership/ACL changes. No DROP or --clean.
    existing = run_tool("psql", ["-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f');"], env, args.pg_bin, True)
    if int(existing.strip()):
        raise ValueError("Restore database must be empty")
    uploads = Path(args.uploads).resolve()
    if uploads.exists() and any(uploads.iterdir()):
        raise ValueError("Restore upload directory must be empty")
    with tempfile.TemporaryDirectory(prefix="codepilot-restore-") as directory:
        stage = Path(directory)
        decrypt_file(args.archive, stage / "payload.tar", encryption_key())
        extracted = stage / "extracted"
        extracted.mkdir()
        with tarfile.open(stage / "payload.tar", "r") as archive:
            seen = set()
            for member in archive:
                name = PurePosixPath(member.name)
                if not member.isfile() or name.is_absolute() or ".." in name.parts or ":" in member.name or "\\" in member.name or member.name in seen or (member.name not in {"database.dump", "manifest.json"} and not member.name.startswith("uploads/")):
                    raise ValueError("Unsafe or duplicate backup member")
                seen.add(member.name)
                destination = extracted.joinpath(*name.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream, open(destination, "xb") as out:
                    shutil.copyfileobj(stream, out)
        manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != 1 or set(manifest["files"]) != seen - {"manifest.json"} or "database.dump" not in manifest["files"]:
            raise ValueError("Invalid backup manifest")
        for name, expected in manifest["files"].items():
            if digest(extracted / name) != expected:
                raise ValueError("Backup checksum mismatch")
        # Stage uploads before restoring SQL; a failed DB restore leaves the
        # operator's target directory empty. Full disk capacity is a prerequisite.
        restored_uploads = extracted / "uploads"
        restored_uploads.mkdir(exist_ok=True)
        run_tool("pg_restore", ["--single-transaction", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", env["PGDATABASE"], str(extracted / "database.dump")], env, args.pg_bin)
        run_tool("psql", ["-X", "-v", "ON_ERROR_STOP=1", "-c", "DO $$ BEGIN IF to_regclass('public.action_drafts') IS NOT NULL THEN UPDATE action_drafts SET status='unknown', error_message='Restored snapshot: reconcile the remote outcome before issuing a new draft' WHERE status IN ('pending_confirmation','executing'); END IF; END $$;"], env, args.pg_bin)
        uploads.mkdir(parents=True, exist_ok=True)
        shutil.copytree(restored_uploads, uploads, dirs_exist_ok=True)
    return {"database_restored": True, "uploads_restored": True, "pending_drafts_require_reconciliation": True, "vector_reindex_required": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "restore"])
    parser.add_argument("--archive", required=True)
    parser.add_argument("--uploads", required=True)
    parser.add_argument("--pg-bin")
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(create(args) if args.action == "create" else restore(args)))
    except Exception as exc:
        # Include no provider diagnostics, URLs or credentials.
        print(json.dumps({"error_type": type(exc).__name__, "message": "Backup/restore failed; target must remain offline until checked"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
