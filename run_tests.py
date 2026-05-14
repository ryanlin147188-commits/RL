"""Run Markdown-based Robot Framework tests.

Workspace adaptation of the original autotest `run_tests.py`. Allure
logic has been removed; the platform now relies on the native Robot
HTML report plus the in-app execution log/screenshots served from
MinIO.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Sequence

from robot import run_cli  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]


STEP_CELL_COUNT = 7
LEGACY_STEP_CELL_COUNT = 9
INPUT_SEPARATOR = ";;"
ASSERTION_OPERATORS = ("==", "!=", "contains", "not contains", ">", ">=", "<", "<=")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="將 Markdown 測試轉成 Robot Framework 後執行")
    parser.add_argument(
        "-f",
        "--file",
        dest="markdown_files",
        action="append",
        default=[],
        help="指定要執行的 Markdown 測試檔，可重複傳入；支援專案相對路徑、tests 相對路徑或絕對路徑。",
    )
    parser.add_argument(
        "-t",
        "--testcase",
        dest="test_cases",
        action="append",
        default=[],
        help="指定要執行的 Test Case 名稱，可重複傳入。",
    )
    parser.add_argument(
        "-o",
        "--outputdir",
        dest="output_dir",
        default=None,
        help="自訂 Robot 輸出目錄，預設為 results/。",
    )
    return parser.parse_args()


def _normalize_legacy_step(cells: List[str], markdown_path: Path) -> List[str]:
    legacy_cells = (cells + [""] * LEGACY_STEP_CELL_COUNT)[:LEGACY_STEP_CELL_COUNT]
    bdd, step_desc, action, *raw_values = legacy_cells

    operator = ""
    expected = ""
    action_values = raw_values
    for index, value in enumerate(raw_values):
        if value in ASSERTION_OPERATORS:
            expected_index = index + 1
            if expected_index >= len(raw_values):
                raise ValueError(f"Markdown test assertion is missing expected value: {markdown_path}")
            operator = value
            expected = raw_values[expected_index]
            action_values = raw_values[:index]
            break

    target = action_values[0].strip() if action_values else ""
    input_values = [value.strip() for value in action_values[1:] if value.strip()]
    input_data = f" {INPUT_SEPARATOR} ".join(input_values)
    return [bdd.strip(), step_desc.strip(), action.strip(), target, input_data, operator, expected]


def _normalize_step_cells(cells: List[str], markdown_path: Path) -> List[str]:
    normalized = [cell.strip() for cell in cells]
    if len(normalized) > STEP_CELL_COUNT:
        return _normalize_legacy_step(normalized, markdown_path)
    return (normalized + [""] * STEP_CELL_COUNT)[:STEP_CELL_COUNT]


def parse_markdown_test(markdown_path: Path) -> Dict[str, Any]:
    resource = "../../tests_resources/core_keywords.resource"
    test_case_name = markdown_path.stem
    documentation = ""
    steps: List[List[str]] = []

    for raw_line in markdown_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("Resource:"):
            resource = line.split(":", 1)[1].strip()
            continue

        if line.startswith("Test Case:"):
            test_case_name = line.split(":", 1)[1].strip()
            continue

        if line.startswith("Documentation:"):
            documentation = line.split(":", 1)[1].strip()
            continue

        if not line.startswith("|"):
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or cells[0].lower() == "bdd":
            continue

        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue

        steps.append(_normalize_step_cells(cells, markdown_path))

    if not steps:
        raise ValueError(f"Markdown test has no executable steps: {markdown_path}")

    return {
        "resource": resource,
        "test_case_name": test_case_name,
        "documentation": documentation,
        "steps": steps,
    }


def build_robot_step_keyword_name(step_index: int, bdd: str, step_desc: str) -> str:
    formatted_index = f"{step_index:03d}"
    parts = [f"步驟 {formatted_index}"]
    if bdd:
        parts.append(f"[{bdd}]")
    if step_desc:
        parts.append(step_desc)
    return " ".join(parts)


def escape_robot_cell_value(value: str) -> str:
    if value.startswith("#"):
        return f"\\{value}"
    return value


def render_robot_test(markdown_path: Path, robot_path: Path) -> str:
    parsed = parse_markdown_test(markdown_path)
    project_root = Path(__file__).resolve().parent
    candidates = [
        markdown_path.parent / parsed["resource"],
        project_root / parsed["resource"],
        project_root / "tests_resources" / "core_keywords.resource",
    ]
    resource_path = next((c.resolve() for c in candidates if c.exists()), candidates[0].resolve())
    relative_resource = os.path.relpath(resource_path, robot_path.parent).replace("\\", "/")

    lines = [
        "*** Settings ***",
        f"Resource    {relative_resource}",
        "",
        "*** Test Cases ***",
        str(parsed["test_case_name"]),
    ]

    documentation = str(parsed["documentation"])
    if documentation:
        lines.append(f"    [Documentation]    {documentation}")

    step_keyword_names: List[str] = []
    for index, step in enumerate(parsed["steps"], start=1):
        step_keyword_name = build_robot_step_keyword_name(index, step[0], step[1])
        step_keyword_names.append(step_keyword_name)
        lines.append(f"    {step_keyword_name}")

    lines.extend(["", "*** Keywords ***"])

    for step_keyword_name, step in zip(step_keyword_names, parsed["steps"]):
        normalized_step = [escape_robot_cell_value(value) if value else "${EMPTY}" for value in step]
        lines.append(step_keyword_name)
        lines.append("    執行通用測試步驟    " + "    ".join(normalized_step))
        lines.append("")

    return "\n".join(lines)


def discover_markdown_tests(tests_dir: Path) -> List[Path]:
    generated_dir = (tests_dir / "generated").resolve()
    return sorted(
        (path.resolve() for path in tests_dir.rglob("*.md") if generated_dir not in path.resolve().parents),
        key=lambda path: str(path),
    )


def resolve_markdown_path(project_root: Path, tests_dir: Path, raw_path: str) -> Path:
    requested_path = Path(raw_path)
    candidates: List[Path] = []

    if requested_path.is_absolute():
        candidates.append(requested_path)
    else:
        candidates.append(project_root / requested_path)
        candidates.append(tests_dir / requested_path)

    tests_root = tests_dir.resolve()
    generated_dir = (tests_dir / "generated").resolve()
    seen_candidates = set()

    for candidate in candidates:
        expanded_candidates = [candidate]
        if candidate.suffix.lower() != ".md":
            expanded_candidates.append(candidate.with_suffix(".md"))

        for expanded_candidate in expanded_candidates:
            resolved_candidate = expanded_candidate.resolve()
            if resolved_candidate in seen_candidates:
                continue
            seen_candidates.add(resolved_candidate)

            if not resolved_candidate.exists() or not resolved_candidate.is_file():
                continue
            if resolved_candidate.suffix.lower() != ".md":
                continue
            if generated_dir in resolved_candidate.parents:
                continue
            try:
                resolved_candidate.relative_to(tests_root)
            except ValueError:
                continue

            return resolved_candidate

    raise ValueError(f"找不到指定的 Markdown 測試檔: {raw_path}")


def resolve_markdown_tests(project_root: Path, tests_dir: Path, raw_paths: Sequence[str]) -> List[Path]:
    if not raw_paths:
        return discover_markdown_tests(tests_dir)

    resolved_paths: List[Path] = []
    seen_paths = set()

    for raw_path in raw_paths:
        resolved_path = resolve_markdown_path(project_root, tests_dir, raw_path)
        if resolved_path in seen_paths:
            continue
        seen_paths.add(resolved_path)
        resolved_paths.append(resolved_path)

    return resolved_paths


def build_generated_tests(tests_dir: Path, markdown_paths: Sequence[Path]) -> List[Path]:
    generated_dir = tests_dir / "generated"
    if generated_dir.exists():
        shutil.rmtree(generated_dir)
    generated_dir.mkdir(parents=True, exist_ok=True)

    tests_root = tests_dir.resolve()
    generated_paths: List[Path] = []
    for markdown_path in markdown_paths:
        relative_markdown_path = markdown_path.resolve().relative_to(tests_root)
        target_dir = generated_dir / relative_markdown_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        robot_path = target_dir / f"{relative_markdown_path.stem}.robot"
        robot_path.write_text(render_robot_test(markdown_path, robot_path), encoding="utf-8")
        generated_paths.append(robot_path.resolve())

    return generated_paths


def build_robot_cli_arguments(
    robot_paths: Sequence[Path],
    output_dir: Path,
    test_cases: Sequence[str],
) -> List[str]:
    arguments = [
        "--outputdir",
        str(output_dir),
        "--name",
        "BDD_自動化測試專案",
    ]

    for test_case in test_cases:
        arguments.extend(["--test", test_case])

    arguments.extend(str(path) for path in robot_paths)
    return arguments


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parent
    tests_dir = project_root / "tests"
    output_dir = Path(args.output_dir).resolve() if args.output_dir else project_root / "results"

    print("[INFO] 啟動自動化測試流程")
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown_paths = resolve_markdown_tests(project_root, tests_dir, args.markdown_files)
    if not markdown_paths:
        raise ValueError(f"找不到可執行的 Markdown 測試檔: {tests_dir}")

    if args.markdown_files:
        print("[INFO] 本次只執行以下 Markdown 測試檔：")
        for markdown_path in markdown_paths:
            print(f"[INFO] - {markdown_path.relative_to(project_root)}")

    if args.test_cases:
        print("[INFO] 本次只執行以下 Test Case：")
        for test_case in args.test_cases:
            print(f"[INFO] - {test_case}")

    generated_paths = build_generated_tests(tests_dir, markdown_paths)
    print(f"[INFO] 已從 Markdown 產生 {len(generated_paths)} 個 Robot 測試檔")

    robot_arguments = build_robot_cli_arguments(generated_paths, output_dir, args.test_cases)
    status = run_cli(arguments=robot_arguments, exit=False)

    if status == 0:
        print("\n[OK] 所有測試案例皆順利通過")
    else:
        print("\n[FAIL] 測試已結束，部分案例失敗或環境尚未設定完成")
    print(f"[INFO] 請開啟 {output_dir / 'log.html'} 檢視詳細報告")


if __name__ == "__main__":
    main()
