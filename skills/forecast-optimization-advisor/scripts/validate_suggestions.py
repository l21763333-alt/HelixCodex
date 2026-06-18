from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


FORBIDDEN = ["可以上线", "must be the root cause", "definitely caused by", "模型效果很好", "模型效果很差"]


def _suggestion_blocks(text: str) -> list[str]:
    matches = list(re.finditer(r"^##\s+建议\s+\d+[:：].*$", text, flags=re.M))
    if not matches:
        return [text]
    blocks = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append(text[start:end])
    return blocks


def validate(text: str) -> list[str]:
    errors: list[str] = []
    for phrase in FORBIDDEN:
        if phrase in text:
            errors.append(f"禁用表达：{phrase}")
    blocks = _suggestion_blocks(text)
    for index, block in enumerate(blocks, start=1):
        for required in ["证据：", "动作：", "验证："]:
            if required not in block:
                errors.append(f"建议 {index} 缺少 {required}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    text = Path(args.input).read_text(encoding="utf-8")
    errors = validate(text)
    if errors:
        print("\n".join(errors))
        return 1
    print("通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
