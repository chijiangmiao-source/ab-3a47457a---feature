"""Inheritance-window analysis over a frozen verdict's step snapshots.

值班员视角：把"某任务有效优先级偏离其基准优先级"的连续步段归并成时段，
免去了逐步翻阅快照。归并只看偏离是否中断——相邻快照即使继承来源任务
改变、偏离幅度变化，只要有效优先级始终偏离基准，就属于同一时段。
"""

from __future__ import annotations


def _task_view(snapshot: dict, task_id: int) -> dict | None:
    for task in snapshot["tasks"]:
        if task["id"] == task_id:
            return task
    return None


def _event_ref(step: dict) -> dict:
    """Pointer back to the step's event so reviewers can cross-check."""
    return {
        "index": step["index"],
        "type": step["type"],
        "detail": step["detail"],
        "raw": step["raw"],
    }


def _evidence(step: dict, task_id: int) -> dict:
    """Wait-chain evidence at one boundary step, replayable against the
    step-by-step snapshots of the same frozen verdict."""
    snap = step["snapshot"]
    task = _task_view(snap, task_id)
    return {
        "stepIndex": step["index"],
        "basePriority": task["basePriority"],
        "effectivePriority": task["effectivePriority"],
        "inheritedFrom": list(task["inheritedFrom"]),
        # Only chains that pass through this task explain its inheritance.
        "chains": [list(c) for c in snap.get("chains", []) if task_id in c],
    }


def inheritance_windows(verdict: dict, task_id: int) -> dict:
    """Group consecutive steps where ``task_id`` runs off-base into windows.

    Expects an *accepted* verdict (every step carries a snapshot) and a task
    that exists in the submission; the store enforces both before calling.

    Each window reports its boundary events, the most urgent inherited
    priority seen inside, wait-chain evidence at both boundaries, and the
    source tasks de-duplicated in first-appearance order.
    """
    windows: list[dict] = []
    current: dict | None = None

    def close() -> None:
        nonlocal current
        start, end = current["startStep"], current["endStep"]
        windows.append(
            {
                "startIndex": start["index"],
                "endIndex": end["index"],
                "startEvent": _event_ref(start),
                "endEvent": _event_ref(end),
                "peakInheritedPriority": current["peak"],
                "sourceTasks": current["sources"],
                "startEvidence": _evidence(start, task_id),
                "endEvidence": _evidence(end, task_id),
            }
        )
        current = None

    base_priority: int | None = None
    for step in verdict["steps"]:
        snap = step["snapshot"]
        task = _task_view(snap, task_id) if snap else None
        if task is None:
            # Accepted verdicts always snapshot every task; treat anything
            # else as "not deviating" and cut any open window.
            deviating = False
        else:
            if base_priority is None:
                base_priority = task["basePriority"]
            deviating = task["effectivePriority"] != task["basePriority"]

        if deviating:
            if current is None:
                current = {"startStep": step, "peak": task["effectivePriority"], "sources": []}
            current["endStep"] = step
            if task["effectivePriority"] < current["peak"]:
                current["peak"] = task["effectivePriority"]
            for src in task["inheritedFrom"]:
                if src not in current["sources"]:
                    current["sources"].append(src)
        elif current is not None:
            close()

    if current is not None:
        close()

    return {"taskId": task_id, "basePriority": base_priority, "windows": windows}
