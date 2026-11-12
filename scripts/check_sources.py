"""Fail CI if remote npm artifacts or production base images leave official sources."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main():
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
    count = 0
    for name, package in lock["packages"].items():
        resolved = package.get("resolved", "")
        if not resolved:
            continue
        if package.get("link") and resolved.startswith("tools/"):
            continue
        parsed = urlsplit(resolved)
        if parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org" or parsed.username or parsed.password:
            raise ValueError(f"Non-official npm artifact: {name}")
        if not package.get("integrity"):
            raise ValueError(f"npm artifact missing integrity: {name}")
        count += 1
    allowed = {"python", "node", "postgres", "nginx", "milvusdb/milvus", "pgvector/pgvector", "redis"}
    image_files = (
        "backend/Dockerfile",
        "frontend/Dockerfile",
        "sandbox/python/Dockerfile",
        "deploy/Backup.Dockerfile",
        "deploy/Gateway.Dockerfile",
        "deploy/Milvus.Dockerfile",
        "deploy/Postgres.Dockerfile",
        "deploy/compose.production.yml",
        "docker-compose.yml",
    )
    for filename in image_files:
        path = ROOT / filename
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:FROM|image:)\s+([^\s]+)", line)
            if not match:
                continue
            image = match[1].strip('"\'')
            if image == "scratch" and line.lstrip().startswith("FROM "):
                continue
            if image.startswith("${"):
                image = image.split(":-", 1)[-1].rstrip("}")
            if image.startswith(("codepilot-api:", "codepilot-web:", "codepilot-backup:", "codepilot-gateway:", "codepilot-milvus:", "codepilot-postgres:")):
                continue  # Built from our reviewed Dockerfiles, never pulled as a fallback.
            publisher = image.split("@", 1)[0].split(":", 1)[0]
            if publisher not in allowed:
                raise ValueError(f"Unapproved production base image in {path.name}")
    print(f"Official source policy passed: {count} npm artifacts with integrity; approved image publishers")


if __name__ == "__main__":
    main()
