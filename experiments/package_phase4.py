"""Build a submission archive from an explicit allowlist; never include .env."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("client", "server", "lb", "experiments", "tests", "texts", "docs", "results/phase4")
ROOT_FILES = ("README.md", "Makefile", "compose.yml", "compose.cluster.yml", ".env.example", ".gitignore")
EXCLUDED = {".git", ".venv", "venv", "__pycache__", ".env", ".DS_Store"}


def source_files(root):
    paths = [root / name for name in ROOT_FILES]
    for name in DIRECTORIES:
        paths.extend((root / name).rglob("*"))
    return sorted({path for path in paths if path.is_file() and not path.is_symlink()
                   and not EXCLUDED.intersection(path.relative_to(root).parts)
                   and not (path.name.startswith(".env") and path.name != ".env.example")
                   and not path.name.endswith((".pyc", ".zip", "-notes.md"))})


def package(root, group_id, output=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", group_id):
        raise ValueError("provide GROUP_ID using letters, digits, underscores or hyphens")
    name = f"lab-{group_id}-phase4"
    output = output or root / "dist" / f"{name}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
    manifest = dict(source_commit=commit or "unavailable", files={})
    # Exclusive creation avoids silently replacing a previous submission.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_files(root):
            relative = str(path.relative_to(root))
            manifest["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            archive.write(path, f"{name}/{relative}")
        archive.writestr(f"{name}/submission-manifest.json", json.dumps(manifest, indent=2) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        print(package(ROOT, args.group_id, args.output))
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
