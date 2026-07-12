from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "", ".py", ".toml", ".yaml", ".yml", ".json", ".md",
    ".txt", ".ini", ".cfg", ".example", ".gitignore",
}
DENY_FILE_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "generic assigned secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"]?(?!\s|\$\{|<|your_|example|none|null)[A-Za-z0-9_./+=-]{12,}"
    ),
    "hard-coded QMT account": re.compile(r"(?i)(?:account[_-]?id|stock[_-]?account)\s*[:=]\s*['\"]?[0-9]{6,}"),
}
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "data", "reports", "runtime"}


def candidate_files() -> list[Path]:
    if not (ROOT / ".git").exists():
        return [path for path in ROOT.rglob("*") if path.is_file() and not IGNORED_PARTS.intersection(path.parts)]
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        )
        return [ROOT / line for line in output.splitlines() if line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [path for path in ROOT.rglob("*") if path.is_file() and not IGNORED_PARTS.intersection(path.parts)]


def main() -> int:
    findings: list[str] = []
    files = candidate_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.suffix.lower() in DENY_FILE_SUFFIXES:
            findings.append(f"禁止的密钥文件: {relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                # Never print the matched secret itself.
                findings.append(f"{relative}:{line}: {label}")
    if findings:
        print("敏感信息扫描失败：", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print(f"敏感信息扫描通过，共检查 {len(files)} 个候选文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
