#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""独立准备冻结协议；本进程不加载候选模型。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from heretic.ara_research_schema import read_json
from heretic.research_protocol import build_research_manifest


def main():
    """接收带许可/版本/清单身份的准备 JSON，输出不可变协议。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        help="独立准备者提供的 JSON 配置，字段见 spec.md",
    )
    args = parser.parse_args()
    try:
        protocol = build_research_manifest(read_json(args.config))
    except (ValueError, OSError, KeyError) as error:
        print(
            json.dumps(
                {
                    "phase": "prepare",
                    "research_status": "failed",
                    "reason": str(error),
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "phase": "prepare",
                "research_status": "not_run",
                "protocol_hash": protocol["protocol_hash"],
                "audit_sample_shortfall": protocol["audit_sample_shortfall"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
