"""Structured change proposals and source/location checks; no code is executed."""

import html
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from indexer.snapshots import load_snapshot
from security.sources import sensitive_path

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)]
Reference = Annotated[str, StringConstraints(strict=True, pattern=r"^S[1-9][0-9]{0,8}$")]
References = Annotated[list[Reference], Field(min_length=1, max_length=20)]

PLANNING_INSTRUCTIONS = (
    "Produce a change proposal for the user's request using submit_change_plan. "
    "First inspect the selected source using the existing tools. Separate observed current behavior "
    "from proposed work. current_behavior and file_changes must use reference IDs from this run. "
    "For modify/delete, read and cite the target file itself. For create, cite the existing integration "
    "point that motivates the new file. All paths are exact repository-relative POSIX paths. "
    "Use modify/create/delete to describe proposed actions; do not claim changes were applied. "
    "Order steps and link each step to file_changes via file_paths; cover every proposed file. "
    "Give concrete validation methods and expected results. Commands are proposals, never executed. "
    "Do not claim tests passed. If evidence is insufficient, leave unsupported changes out and list "
    "open_questions rather than inventing files or behavior. Use the user's language."
)


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CurrentBehavior(PlanItem):
    description: Text
    references: References


class FileChange(PlanItem):
    file_path: Text
    action: Literal["modify", "create", "delete"]
    reason: Text
    changes: Annotated[list[Text], Field(min_length=1, max_length=20)]
    references: References


class ImplementationStep(PlanItem):
    description: Text
    file_paths: Annotated[list[Text], Field(min_length=1, max_length=20)]


class ValidationStep(PlanItem):
    method: Text
    expected_result: Text
    command: Text | None = None


class ChangePlan(PlanItem):
    """Source-backed current behavior, proposed files, ordered steps and validation."""

    goal: Text
    current_behavior: Annotated[list[CurrentBehavior], Field(max_length=20)]
    file_changes: Annotated[list[FileChange], Field(max_length=20)]
    steps: Annotated[list[ImplementationStep], Field(max_length=30)]
    validation: Annotated[list[ValidationStep], Field(max_length=20)]
    open_questions: Annotated[list[Text], Field(max_length=20)]


def _prose(value):
    # Model fields are data. Only the program adds markup and source links.
    value = html.escape(" ".join(value.split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", value)


def _references(items):
    return " ".join(f"[{item}]" for item in dict.fromkeys(items))


def render_plan(plan: ChangePlan) -> str:
    lines = ["### 修改目标", "", _prose(plan.goal)]
    if plan.current_behavior:
        lines += ["", "### 当前实现", ""]
        lines += [f"- {_prose(item.description)} {_references(item.references)}" for item in plan.current_behavior]
    if plan.file_changes:
        lines += ["", "### 涉及文件", ""]
        actions = {"modify": "修改", "create": "新增", "delete": "删除"}
        for item in plan.file_changes:
            lines += [f"**{_prose(item.file_path)} · {actions[item.action]}**", "",
                      f"{_prose(item.reason)} {_references(item.references)}", ""]
            lines += [f"- {_prose(change)}" for change in item.changes]
            lines.append("")
    if plan.steps:
        lines += ["", "### 修改顺序", ""]
        for number, step in enumerate(plan.steps, 1):
            paths = "、".join(_prose(path) for path in step.file_paths)
            lines += [f"{number}. {_prose(step.description)}（{paths}）"]
    if plan.validation:
        lines += ["", "### 验证方法", ""]
        for number, item in enumerate(plan.validation, 1):
            lines += [f"{number}. {_prose(item.method)}；预期：{_prose(item.expected_result)}"]
            if item.command:
                # Longer than any contained backtick run, so commands remain code.
                fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", item.command)), default=0))
                lines += ["", fence, item.command, fence, ""]
    if plan.open_questions:
        lines += ["", "### 待确认", ""]
        lines += [f"- {_prose(question)}" for question in plan.open_questions]
    return "\n".join(lines).strip()


def _valid_path(value):
    path = PurePosixPath(value)
    return bool(value and not path.is_absolute() and not PureWindowsPath(value).drive
                and "\\" not in value and all(ord(char) >= 32 and ord(char) != 127 for char in value)
                and path.as_posix() == value and value != "."
                and not any(part in {".", ".."} or part.lower() == ".git" for part in path.parts))


def check_plan(plan: ChangePlan, evidence):
    from agent.evidence import CitationIssue

    checked = evidence.check_answer(render_plan(plan))
    issues = []

    def issue(message):
        issues.append(CitationIssue(message=message))

    snapshot = load_snapshot(evidence.repo_root)
    if (snapshot.repo_url, snapshot.commit_sha) != (evidence.repo_url, evidence.commit_sha):
        raise ValueError("规划与当前仓库或代码版本不一致。")
    sources = {source.reference_id: source for source in checked.citations}
    if not plan.current_behavior:
        issue("规划缺少当前实现的源码依据。")
    if not plan.file_changes:
        issue("尚未确定修改文件。")
    if not plan.validation:
        issue("规划缺少验证方法和预期结果。")
    targets = set()
    for change in plan.file_changes:
        path = change.file_path
        if not _valid_path(path):
            issue(f"修改路径无效：{path}")
            continue
        if sensitive_path(path):
            issue(f"安全策略禁止规划敏感路径：{path}")
            continue
        if path in targets:
            issue(f"修改文件重复：{path}")
        targets.add(path)
        available = [sources[reference] for reference in change.references if reference in sources]
        if change.action == "create":
            # A new path cannot overlap a committed file, directory, or skipped Git object.
            known = set(snapshot.files) | set(snapshot.skipped)
            if any(path == item or item.startswith(path + "/") or path.startswith(item + "/") for item in known):
                issue(f"新增路径与当前快照已有内容冲突：{path}")
            if not available:
                issue(f"新增文件缺少已核对的接入位置依据：{path}")
        else:
            if path not in snapshot.files:
                issue(f"修改或删除的文件不在当前快照中：{path}")
            if not any(source.file_path == path for source in available):
                issue(f"修改或删除的文件缺少本文件源码依据：{path}")
    covered = set()
    for number, step in enumerate(plan.steps, 1):
        for path in step.file_paths:
            if path not in targets:
                issue(f"第 {number} 步引用了未列入修改清单的文件：{path}")
            else:
                covered.add(path)
    for path in sorted(targets - covered):
        issue(f"修改顺序未覆盖文件：{path}")
    text = checked.text
    if checked.issues or issues:
        text += "\n\n### 规划检查\n\n" + "\n".join(
            f"- {_prose(item.message)}" for item in (*checked.issues, *issues)
        )
    return checked.model_copy(update={"text": text, "issues": (*checked.issues, *issues), "plan": plan})
