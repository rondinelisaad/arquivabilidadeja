from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from archivability.validation.study import (
    ValidationInputError,
    analyze_validation_dataset,
    load_validation_dataset,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archivability-validation",
        description="Generate a deterministic descriptive validation report.",
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = analyze_validation_dataset(load_validation_dataset(arguments.dataset))
        rendered = (
            json.dumps(
                report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n"
        )
        if arguments.output is None:
            sys.stdout.write(rendered)
        else:
            arguments.output.write_text(rendered, encoding="utf-8")
    except (ValidationInputError, OSError) as exc:
        parser.exit(2, f"validation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
