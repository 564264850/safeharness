"""Small composable host: model, tools, verification, state and one Runtime loop."""

from .checkpoints import CheckpointSession
from .runtime import run_task


class Harness:
    def __init__(
        self,
        store,
        *,
        model,
        execute,
        verify,
        configuration,
        workspace=None,
        stateless_model=False,
        max_steps=8,
        timeout_seconds=120,
        complete=None,
        approval_workspace=None,
        approval_mode="agent",
        max_repeated_failures=3,
    ):
        self.store, self.model, self.execute, self.verify = (
            store,
            model,
            execute,
            verify,
        )
        self.configuration, self.workspace = configuration, workspace
        self.stateless_model, self.max_steps = stateless_model, max_steps
        self.timeout_seconds, self.complete = timeout_seconds, complete
        self.approval_workspace, self.approval_mode = approval_workspace, approval_mode
        self.max_repeated_failures = max_repeated_failures

    def _checkpoint(self, task_id):
        return CheckpointSession(
            self.store,
            task_id,
            (
                {
                    "application": self.configuration,
                    "max_repeated_failures": self.max_repeated_failures,
                }
                if self.approval_mode == "agent"
                else self.configuration
            ),
            model=self.model,
            workspace=self.workspace,
            stateless_model=self.stateless_model,
            timeout_seconds=self.timeout_seconds,
            approval_mode=self.approval_mode,
        )

    def run(self, task, *, pause_after=None, initialize_running=False):
        return run_task(
            self.model,
            self.execute,
            self.verify,
            max_steps=self.max_steps,
            store=self.store,
            task=task,
            complete=self.complete,
            checkpoint=self._checkpoint(task.task_id),
            pause_after=pause_after,
            initialize_running=initialize_running,
            max_repeated_failures=self.max_repeated_failures,
        )

    def resume(
        self,
        task_id,
        *,
        recover_abandoned=False,
        expected_generation=None,
        pause_after=None,
        after_business_commit=None,
    ):
        return run_task(
            self.model,
            self.execute,
            self.verify,
            max_steps=self.max_steps,
            store=self.store,
            complete=self.complete,
            checkpoint=self._checkpoint(task_id),
            resume=True,
            recover_abandoned=recover_abandoned,
            expected_generation=expected_generation,
            pause_after=pause_after,
            approval_workspace=self.approval_workspace,
            after_business_commit=after_business_commit,
            max_repeated_failures=self.max_repeated_failures,
        )
