"""L08 工作台增量：证据信封（envelope）。

把 Harness 报告的运行身份与内容指纹绑成一份可核对记录。
记录：提交 SHA、Run ID、工作流、系统、Python、套件、报告 SHA-256、报告判决。
边界（如实标注，勿夸大）：只绑定来源与哈希，不替代报告合同校验，也不判断业务是否正确；
报告为空对象时仍会生成缺少有效业务结论的信封。缺运行身份与报告缺失一律拒绝。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ENV_KEYS = (
    "GITHUB_SHA",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_WORKFLOW",
    "GITHUB_JOB",
    "GITHUB_REF",
    "GITHUB_EVENT_NAME",
    "GITHUB_REPOSITORY",
    "GITHUB_SERVER_URL",
)
REQUIRED_IDENTITY = ("GITHUB_SHA", "GITHUB_RUN_ID")
VALID_DECISIONS = {"pass", "block"}


def report_sha256(report_path: str | Path) -> str:
    return hashlib.sha256(Path(report_path).read_bytes()).hexdigest()


def build_envelope(report_path: str | Path, env: dict | None = None, output_path: str | Path | None = None) -> dict:
    env = dict(os.environ if env is None else env)
    report = Path(report_path)
    if not report.is_file():
        raise FileNotFoundError(f"报告不存在：{report}")
    identity = {key: env[key] for key in ENV_KEYS if env.get(key)}
    missing = [key for key in REQUIRED_IDENTITY if not identity.get(key)]
    if missing:
        raise SystemExit("信封拒绝生成：缺少运行身份 " + "、".join(missing))
    raw = report.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    summary = payload.get("summary") if isinstance(payload, dict) else None
    decision = summary.get("decision") if isinstance(summary, dict) else None
    envelope = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
        "workflow": env.get("GITHUB_WORKFLOW", ""),
        "system": platform.platform(),
        "python": sys.version.split()[0],
        "suite": payload.get("suite") if isinstance(payload, dict) else None,
        "report_path": str(report),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "report_decision": decision,
        "report_summary": summary if isinstance(summary, dict) else None,
        "has_valid_business_conclusion": decision in VALID_DECISIONS,
    }
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    return envelope


def quarantine(output: Path) -> Path | None:
    if not output.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stale = output.with_name(f"{output.name}.stale-{stamp}")
    shutil.move(str(output), str(stale))
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description="证据信封：把运行身份与报告哈希绑成一份记录")
    parser.add_argument("--report", required=True, help="Harness 报告路径")
    parser.add_argument("--output", required=True, help="信封输出路径")
    args = parser.parse_args()
    output = Path(args.output)
    try:
        envelope = build_envelope(args.report, output_path=output)
    except (FileNotFoundError, SystemExit, json.JSONDecodeError, UnicodeDecodeError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        stale = quarantine(output)
        print(f"[ci-evidence] 拒绝生成信封：{reason}", file=sys.stderr)
        if stale is not None:
            print(f"[ci-evidence] 旧输出已隔离为 {stale.name}，未留在本次输出路径", file=sys.stderr)
        return 1
    print(json.dumps({
        "output": str(output),
        "identity": envelope["identity"],
        "report_sha256": envelope["report_sha256"],
        "report_decision": envelope["report_decision"],
        "has_valid_business_conclusion": envelope["has_valid_business_conclusion"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())