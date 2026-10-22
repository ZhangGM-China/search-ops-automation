from typing import Iterable, Optional


def select_task_winner(tasks: Iterable[object], action_task_ids: set[int]) -> Optional[object]:
    variants = list(tasks)
    if not variants:
        return None
    active = [task for task in variants if str(getattr(task, "status", "") or "") != "superseded"] or variants
    return min(active, key=lambda task: (
        0 if str(getattr(task, "status", "") or "") == "completed" else 1,
        0 if int(getattr(task, "id")) in action_task_ids else 1,
        int(getattr(task, "id")),
    ))
