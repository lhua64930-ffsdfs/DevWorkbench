"""Lock a verified Linux image inventory against wheel hashes from official PyPI.

Input: `pip freeze` and JSON-encoded packaging.tags.sys_tags() from that image.
This fetches metadata only; it does not install packages or execute downloaded code.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
from packaging.utils import canonicalize_name, parse_wheel_filename


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--tags", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache", default=".data/lock-cache")
    args = parser.parse_args()
    packages = []
    for line in Path(args.inventory).read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.startswith("#"):
            if line.count("==") != 1:
                raise ValueError("Only exact PyPI package versions may enter this lock")
            packages.append(tuple(line.strip().split("==")))
    tags = set(json.loads(Path(args.tags).read_text(encoding="utf-8-sig")))
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    def lock(package):
        name, version = package
        cached = cache / (hashlib.sha256(f"{name}=={version}".encode()).hexdigest() + ".json")
        if cached.exists():
            release = json.loads(cached.read_text(encoding="utf-8"))
        else:
            for attempt in range(3):
                try:
                    with urlopen(f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json", timeout=30) as response:
                        release = json.load(response)
                    cached.write_text(json.dumps(release), encoding="utf-8")
                    break
                except (OSError, TimeoutError):
                    if attempt == 2:
                        raise
                    time.sleep(1)
        if canonicalize_name(release["info"]["name"]) != canonicalize_name(name) or release["info"]["version"] != version:
            raise ValueError("Unexpected PyPI release metadata")
        hashes = set()
        for artifact in release["urls"]:
            if artifact["filename"].endswith(".whl"):
                _, _, _, wheel_tags = parse_wheel_filename(artifact["filename"])
                if tags.intersection(str(tag) for tag in wheel_tags):
                    if not artifact["url"].startswith("https://files.pythonhosted.org/"):
                        raise ValueError("Non-official artifact host")
                    hashes.add(artifact["digests"]["sha256"])
        if not hashes:
            raise ValueError(f"No matching official binary wheel: {name}")
        return f"{canonicalize_name(name)}=={version}" + "".join(" \\\n    --hash=sha256:" + value for value in sorted(hashes))
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lock, sorted(packages, key=lambda item: canonicalize_name(item[0]))))
    Path(args.output).write_text("# CPython 3.12, Linux/amd64. Generated from a verified image; official PyPI wheel hashes.\n" + "\n".join(rows) + "\n", encoding="utf-8")
    print(f"Locked {len(rows)} exact package versions with official binary wheel hashes")


if __name__ == "__main__":
    main()
