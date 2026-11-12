"""Generate independent secrets without overwriting a deployment configuration."""
import argparse
import base64
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="Internal HTTPS origin, including port if needed")
    parser.add_argument("--output", default=str(ROOT / "deploy/production.env"))
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
        parser.error("Provide a single HTTPS origin")
    values = {key: secrets.token_urlsafe(48) for key in ("POSTGRES_PASSWORD", "APP_DB_PASSWORD", "MIGRATION_DB_PASSWORD", "TOKEN_ENCRYPTION_KEY", "GITHUB_WEBHOOK_SECRET", "MILVUS_ROOT_PASSWORD", "MILVUS_APP_PASSWORD")}
    values.update(CORS_ORIGINS=args.origin.rstrip("/"), BACKUP_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode())
    values["MILVUS_TOKEN"] = "codepilot:" + values["MILVUS_APP_PASSWORD"]
    text = (ROOT / "deploy/production.env.example").read_text(encoding="utf-8")
    lines = [line.split("=", 1)[0] + "=" + values[line.split("=", 1)[0]] if "=" in line and line.split("=", 1)[0] in values else line for line in text.splitlines()]
    output = Path(args.output)
    with open(output, "x", encoding="utf-8", newline="\n") as stream:
        os.chmod(output, 0o600)
        stream.write("\n".join(lines) + "\n")
    print("Generated private deployment configuration. Supply provider keys and trusted TLS certificates before starting.")


if __name__ == "__main__":
    main()
