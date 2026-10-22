"""Retirement boundary for the pre-rewrite Top Query Agent Loop."""

LEGACY_AGENT_LOOP_JOB_ID = "agent_loop_governance_job"


def legacy_agent_loop_retirement_message() -> str:
    return "top_query_agent_loop_retired"


class LegacyAgentLoopRetiredError(RuntimeError):
    """Raised when an old Agent Loop entrypoint is invoked after cutover."""

    def __init__(self) -> None:
        super().__init__(legacy_agent_loop_retirement_message())
