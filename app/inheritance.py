"""Inheritance-interval audit over a frozen verdict.

A duty officer reviewing a frozen shared-bus lock verdict needs to know
*when* a given task was actually subject to priority inheritance across
the whole trajectory, instead of paging through every step snapshot.

This module derives the **continuous event intervals** during which a
task's effective priority deviates from its base priority:

* Adjacent snapshots stay inside the same interval as long as the
  deviation persists - even when the waiters driving the inheritance
  (the source tasks) change between snapshots.
* Source tasks are listed de-duplicated by first appearance inside the
  interval.
* Both endpoints carry the wait-chain evidence found in the frozen
  snapshots (chains touching the task plus its own waiter/holder state),
  so every interval can be re-verified at its start and end events.

The audit is computed strictly from the already-frozen step snapshots;
it never re-runs or mutates the verdict.
"""

from __future__ import annotations

from typing import Any


class AuditError(Exception):
    """Operator-facing audit failure with a stable API code and status."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}


def _task_by_id(snapshot: dict, tid: int) -> dict:
    for task in snapshot["tasks"]:
        if task["id"] == tid:
            return task
    raise AuditError("task_not_found", f"任务 {tid} 不存在于该审计的快照中。", 404)


def _evidence(step: dict, tid: int) -> dict:
    """Wait-chain evidence reviewable at one interval endpoint."""
    snapshot = step["snapshot"]
    task = _task_by_id(snapshot, tid)
    return {
        "stepIndex": step["index"],
        "event": step["raw"],
        "eventType": step["type"],
        "detail": step["detail"],
        "taskState": {
            "id": tid,
            "state": task["state"],
            "basePriority": task["basePriority"],
            "effectivePriority": task["effectivePriority"],
            "inheritedFrom": list(task["inheritedFrom"]),
            "waitingFor": task["waitingFor"],
            "holdingLocks": list(task["holdingLocks"]),
        },
        # Only the nested wait chains that actually touch this task are
        # relevant (and reviewable against the frozen step snapshot).
        "waitChains": [list(chain) for chain in snapshot.get("chains", []) if tid in chain],
    }


def inheritance_report(verdict: dict, tid: int) -> dict:
    """Compute the continuous inheritance intervals for one task.

    Returns the report payload or raises :class:`AuditError` with an
    actionable, operator-facing Chinese message.
    """
    if not isinstance(verdict, dict):
        raise AuditError("not_found", "冻结裁决不存在。", 404)

    steps = verdict.get("steps") or []
    if not verdict.get("accepted"):
        # A freeze caused by an illegal event voids the submission: the
        # final state and a complete snapshot trail do not exist, so an
        # interval audit over "the whole trajectory" is impossible.
        raise AuditError(
            "verdict_frozen",
            "该裁决因非法事件而冻结，此前旧成功均已作废且无完整快照，"
            "无法审计继承时段；"
            f"非法事件定位在第 {verdict.get('errorIndex')} 项，"
            "请修正事件序列后使用新的审计标识重新提交。",
            409,
            {"errorIndex": verdict.get("errorIndex"), "errorDetail": verdict.get("error")},
        )
    if not steps or steps[0].get("snapshot") is None:
        raise AuditError(
            "verdict_frozen", "冻结裁决缺少基准快照，无法审计继承时段。", 409
        )

    # The init snapshot enumerates every task the audit was submitted with.
    known = [task["id"] for task in steps[0]["snapshot"]["tasks"]]
    if tid not in known:
        raise AuditError(
            "task_not_found",
            f"任务 {tid} 不存在于该审计；请从裁决已有任务中选择：{known}。",
            404,
            {"availableTaskIds": known},
        )

    intervals: list[dict] = []
    current: dict | None = None

    def flush(seg: dict) -> None:
        intervals.append(
            {
                "startEventIndex": seg["start"]["index"],
                "endEventIndex": seg["end"]["index"],
                "startEvent": seg["start"]["raw"],
                "endEvent": seg["end"]["raw"],
                "startDetail": seg["start"]["detail"],
                "endDetail": seg["end"]["detail"],
                # Smaller numeric value = more urgent.
                "mostUrgentPriority": seg["mostUrgent"],
                "sourceTaskIds": seg["sources"],
                "startEvidence": _evidence(seg["start"], tid),
                "endEvidence": _evidence(seg["end"], tid),
            }
        )

    for step in steps:
        snapshot = step.get("snapshot")
        # The init step (raw is None) carries no event and can never show
        # inheritance; a frozen step carries no snapshot. Either simply
        # breaks continuity - it can never be an interval endpoint.
        if snapshot is None or step.get("raw") is None:
            boosted = False
            task_state = None
        else:
            task_state = _task_by_id(snapshot, tid)
            boosted = task_state["effectivePriority"] != task_state["basePriority"]

        if boosted and task_state is not None:
            if current is None:
                current = {
                    "start": step,
                    "end": step,
                    "mostUrgent": task_state["effectivePriority"],
                    "sources": [],
                }
            current["end"] = step
            if task_state["effectivePriority"] < current["mostUrgent"]:
                current["mostUrgent"] = task_state["effectivePriority"]
            # inheritedFrom is already arrival-ordered per snapshot; keep
            # the segment list de-duplicated by first appearance.
            for src in task_state["inheritedFrom"]:
                if src not in current["sources"]:
                    current["sources"].append(src)
        elif current is not None:
            flush(current)
            current = None

    if current is not None:
        flush(current)

    final_task = _task_by_id(verdict["finalSnapshot"], tid)
    return {
        "taskId": tid,
        "basePriority": final_task["basePriority"],
        "intervalCount": len(intervals),
        "intervals": intervals,
    }
