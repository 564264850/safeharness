"""OpenJudge is an optional evaluation consumer, never a tool authorization source."""

import asyncio
import json
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import UUID, uuid4

from .dataset import load_dataset
from .evaluation import (
    environment_manifest,
    metrics,
    read_evaluation,
    redact,
    run_case,
    write_report,
)
from .execution import public_tool_schemas
from .provider import MODEL, ProviderError
from .task import TaskStatus
from .task_store import TaskStore
from .trace import TraceLog


def judge_version():
    try:
        installed = version("py-openjudge")
    except PackageNotFoundError:
        raise ValueError(
            "OpenJudge is not installed; run .tools/uv.exe sync --extra judge --locked"
        ) from None
    if installed != "0.2.4":
        raise ValueError("This adapter requires the locked py-openjudge==0.2.4")
    return installed


def make_model(client, task_id, trace):
    judge_version()
    from openjudge.models import BaseChatModel
    from openjudge.models.schema.oai.response import ChatResponse

    class BoundedJudgeModel(BaseChatModel):
        def __init__(self):
            super().__init__(
                model=MODEL if client else "contract-fixture", stream=False
            )
            self.last_error = None

        def __deepcopy__(self, memo):
            return self

        async def achat(self, messages, structured_model=None, **kwargs):
            if client:
                normalized = [
                    message.to_dict() if hasattr(message, "to_dict") else message
                    for message in messages
                ]
                try:
                    response = client.complete(
                        normalized,
                        task_id=task_id,
                        trace=trace,
                        response_json=True,
                        deadline=time.monotonic() + 50,
                    )
                except ProviderError as exc:
                    self.last_error = exc.code
                    raise
                content = response["content"]
                try:
                    parsed = json.loads(content)
                except (ValueError, TypeError):
                    self.last_error = "judge_invalid_json"
                    raise ValueError("judge_invalid_json") from None
            else:
                parsed = {
                    "score": 5,
                    "reason": "Fixed contract fixture; this is not an LLM quality judgement.",
                }
                content = json.dumps(parsed)
            if structured_model is not None:
                try:
                    parsed = structured_model.model_validate(parsed).model_dump()
                except ValueError:
                    self.last_error = "judge_invalid_schema"
                    raise ValueError("judge_invalid_schema") from None
            return ChatResponse(role="assistant", content=content, parsed=parsed)

    return BoundedJudgeModel()


def action_messages(actions):
    messages = []
    for index, action in enumerate(actions):
        call_id = "call_" + str(index)
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": action["tool"],
                                "arguments": json.dumps(action["arguments"]),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {"status": action["status"], "code": action["code"]}
                    ),
                },
            ]
        )
    return messages


async def grade_one(result, *, client, judge_database):
    from openjudge.graders.agent import (
        ActionLoopDetectionGrader,
        ToolCallAccuracyGrader,
        ToolSelectionGrader,
    )

    private = read_evaluation(
        Path(result["database"]), UUID(result["task_id"]), private=True
    )
    store = TaskStore(judge_database)
    store.initialize()
    task = store.transition(store.create(), TaskStatus.RUNNING)
    trace = TraceLog(store.database, task.task_id)
    model = make_model(client, task.task_id, trace)
    calls = [
        {"name": action["tool"], "arguments": action["arguments"]}
        for action in private["actions"]
    ]
    environment = private.get("environment")
    if environment is None:
        environment = next(
            (
                case.environment
                for case in load_dataset()[0].cases
                if case.case_id == result["case_id"]
            ),
            None,
        )
    if environment not in {"ticket", "coding"}:
        raise ValueError("Missing grading environment contract")
    definitions = [tool["function"] for tool in public_tool_schemas(environment)]
    query = (
        "Trusted scope: "
        + json.dumps(private.get("scope", {}))
        + "\nTask: "
        + private["instruction"]
    )
    loop = await ActionLoopDetectionGrader().aevaluate(
        messages=action_messages(private["actions"])
    )
    grades = {}
    for name, grader in (
        ("tool_selection", ToolSelectionGrader(model=model)),
        ("tool_call_accuracy", ToolCallAccuracyGrader(model=model)),
    ):
        grade = await grader.aevaluate(
            query=query, tool_definitions=definitions, tool_calls=calls
        )
        score = getattr(grade, "score", None)
        valid = (
            model.last_error is None
            and isinstance(score, (int, float))
            and 1 <= score <= 5
        )
        grades[name] = {
            "status": "graded" if valid else "failed",
            "score": score if valid else None,
            "reason": redact(str(getattr(grade, "reason", "")))[:2000],
            "error": model.last_error if model.last_error else None,
        }
        if model.last_error:
            break
    task = store.transition(task, TaskStatus.VERIFYING)
    valid = len(grades) == 2 and all(
        grade["status"] == "graded" for grade in grades.values()
    )
    trace.emit(
        "verification",
        {
            "passed": valid,
            "code": "openjudge_contract",
            "case_count": len(grades) + 1,
            "verifier_version": "openjudge-bridge-v1",
        },
    )
    task = store.transition(task, TaskStatus.SUCCEEDED if valid else TaskStatus.FAILED)
    selection = grades.get("tool_selection", {}).get("score")
    return {
        "case_id": result["case_id"],
        "task_id": result["task_id"],
        "judge_task_id": str(task.task_id),
        "judge_database": str(store.database),
        "judge_provider": "deepseek" if client else "contract-fixture",
        "deterministic_contract_passed": result["contract_passed"],
        "graders": grades,
        "action_loop_score": getattr(loop, "score", None),
        "selection_and_task_outcome_differ": (selection >= 4)
        != result["contract_passed"]
        if selection is not None
        else None,
        "usage": client.ledger.usage(task.task_id)
        if client
        else {"calls": 0, "cost_upper_cny": "0"},
    }


def grade_report(report_path: Path, *, client=None, case_ids=None, max_cases=2):
    if not 1 <= max_cases <= 2:
        raise ValueError("At most two observed cases may be graded per invocation")
    installed = judge_version()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    eligible = [
        result
        for result in report["results"]
        if result["category"] != "control" and result["tool_results"]
    ]
    selected = [
        result for result in eligible if not case_ids or result["case_id"] in case_ids
    ][:max_cases]
    if not selected or (
        case_ids and set(case_ids) != {result["case_id"] for result in selected}
    ):
        raise ValueError(
            "Select eligible observed cases within the two-case grading budget"
        )
    output = {
        "schema_version": 1,
        "source_report": str(report_path.resolve()),
        "openjudge_version": installed,
        "bridge_version": "openjudge-bridge-v1",
        "judge_provider": "deepseek" if client else "contract-fixture",
        "note": "Tool choice quality and independent task completion measure different properties. Grader scores do not authorize actions or override task outcomes.",
        "results": [],
    }
    for result in selected:
        output["results"].append(
            asyncio.run(
                grade_one(
                    result,
                    client=client,
                    judge_database=report_path.parent / "judge-tasks.sqlite3",
                )
            )
        )
    destination = report_path.with_name("judge-" + str(uuid4()) + ".json")
    write_report(destination, output)
    return {"judge_report": str(destination), **output}


def counterexample_report(output_dir: Path):
    from .coding_demo import BROKEN

    dataset, digest = load_dataset()
    original = next(
        case for case in dataset.cases if case.case_id == "normal_coding_fix"
    )
    actions = [
        action.model_copy(
            update={"arguments": {"path": "solution.py", "content": BROKEN}}
        )
        if action.name == "write_file"
        else action
        for action in original.script
    ]
    case = original.model_copy(
        update={"case_id": "counterexample_clamp_bad_fix", "script": actions}
    )
    directory = output_dir.resolve() / str(uuid4())
    result = run_case(case, directory / case.case_id)
    report = {
        "schema_version": 1,
        "dataset_version": "m3-judge-probe-v1",
        "split": "contract-probe",
        "provider": "scripted-counterexample",
        "manifest": environment_manifest(digest, "scripted"),
        "results": [result],
        "metrics": metrics([result]),
        "complete": True,
        "note": "Deliberately retains the bug while selecting the appropriate file-edit tool; excluded from the 16-case development dataset.",
    }
    path = directory / "report.json"
    write_report(path, report)
    return {
        "report_json": str(path),
        "deterministic_contract_passed": result["contract_passed"],
    }
