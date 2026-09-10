"""AF7 vendor-neutral reference agent client (§7, §10).

A deterministic client any agent framework can wrap. It performs the external
confirmation protocol end to end: discover capabilities and schemas, read the
revisioned state, draft a plan, obtain (simulated) external confirmation,
authorize, execute, stream events, cancel, and iterate on QA. It contains no
LLM SDK and no model-vendor dependency — the *decision* of what to propose
belongs to the caller; this client validates and executes.
"""
from __future__ import annotations

from dataclasses import dataclass


class ConfirmationCallback:
    """Represents the human-in-the-loop confirmation step owned by the chatbot.

    The default auto-approves (for headless/deterministic tests); a real agent
    supplies a callback that shows the normalized diff and collects a decision.
    """

    def __init__(self, decide=None):
        self._decide = decide or (lambda plan: True)

    def approve(self, plan: dict) -> bool:
        return bool(self._decide(plan))


@dataclass
class PlanRun:
    plan: dict
    authorization: dict | None = None
    result: dict | None = None
    approved: bool = False
    stopped_reason: str | None = None

    def to_dict(self) -> dict:
        return {'plan_id': self.plan.get('plan_id'), 'approved': self.approved,
                'confirmation_class': self.plan.get('confirmation_class'),
                'state': (self.result or {}).get('plan', {}).get('state'),
                'stopped_reason': self.stopped_reason}


class AgentClient:
    def __init__(self, transport, *, confirmation: ConfirmationCallback | None = None,
                 confirmed_by: str = 'external-user'):
        self._transport = transport
        self._confirmation = confirmation or ConfirmationCallback()
        self._confirmed_by = confirmed_by

    # -- discovery + state ------------------------------------------------- #

    async def discover(self) -> dict:
        return {
            'capabilities': await self._transport.capabilities(),
            'fields': await self._transport.fields(),
            'operations': await self._transport.operations(),
        }

    async def read_state(self, case_id: str) -> dict:
        return await self._transport.snapshot(case_id)

    async def create_case(self, path: str) -> dict:
        return await self._transport.create(path)

    async def open_case(self, path: str) -> dict:
        return await self._transport.open(path)

    async def events_since(self, case_id: str, after_sequence: int) -> list:
        return await self._transport.events(case_id, after_sequence)

    # -- the external confirmation protocol (§6.5) ------------------------- #

    async def propose(self, case_id: str, commands: list) -> dict:
        """Draft + validate a plan; the transport returns the normalized diff,
        estimate, impact, confirmation class, and digest."""
        return await self._transport.plan(case_id, commands)

    async def run(self, case_id: str, commands: list) -> PlanRun:
        """Full flow: draft -> validate -> confirm -> authorize -> execute.

        Confirmation is delegated to the injected callback; if it declines, the
        plan is never authorized or executed.
        """
        plan = await self.propose(case_id, commands)
        run = PlanRun(plan=plan)
        if plan.get('confirmation_required') and not self._confirmation.approve(plan):
            run.stopped_reason = 'confirmation_declined'
            return run
        run.approved = True
        run.authorization = await self._transport.confirm(
            case_id, plan['plan_id'], plan['digest'], confirmed_by=self._confirmed_by)
        run.result = await self._transport.execute(
            case_id, plan['plan_id'], run.authorization['token'])
        return run

    async def cancel(self, case_id: str, plan_id: str) -> dict:
        return await self._transport.cancel(case_id, plan_id)

    async def close(self, case_id: str) -> dict:
        return await self._transport.close(case_id)

    # -- high-level flows (§7 documented journeys) ------------------------- #

    async def edit_fields(self, case_id: str, patch: dict) -> PlanRun:
        """Field-edit flow: a single reversible configuration patch."""
        return await self.run(case_id, [
            {'operation': 'configuration.patch', 'parameters': {'patch': patch}}])

    async def remesh(self, case_id: str, patch: dict) -> PlanRun:
        """Full-mesh flow: configure, run stages, inspect, and check."""
        from foammesh.core.automation.recipes import remesh_recipe
        return await self.run(case_id, remesh_recipe(patch).commands())

    async def transform_with_recovery(self, case_id: str, *, kind: str, parameters: dict) -> PlanRun:
        """Transform/recovery flow: a recovery-backed mesh transform."""
        return await self.run(case_id, [
            {'operation': f'mesh.transform.{kind}', 'parameters': parameters}])

    async def adjust_quality(self, case_id: str, *, target_non_ortho: float,
                             tighten: dict) -> dict:
        """QA-adjustment flow: read the quality report; if it misses the target,
        propose a tightening patch and a re-check. Deterministic — the caller
        chooses ``tighten``; the client validates, confirms, and executes."""
        from foammesh.core.automation import quality_adjustment_recipe, quality_assessment_recipe
        assessment = await self.run(case_id, quality_assessment_recipe().commands())
        adjusted = await self.run(case_id, quality_adjustment_recipe(tighten).commands())
        return {'target_non_ortho': target_non_ortho, 'assessment': assessment.to_dict(),
                'adjustment': adjusted.to_dict()}

    async def import_geometry(self, case_id: str, source: str) -> PlanRun:
        from foammesh.core.automation import geometry_import_recipe
        return await self.run(case_id, geometry_import_recipe(source).commands())

    async def build_authored_mesh(self, case_id: str, patch: dict) -> PlanRun:
        from foammesh.core.automation import authored_mesh_recipe
        return await self.run(case_id, authored_mesh_recipe(patch).commands())

    async def import_converter(self, case_id: str, source: str, format_: str) -> PlanRun:
        from foammesh.core.automation import converter_import_recipe
        return await self.run(case_id, converter_import_recipe(source, format_).commands())

    async def export_save_archive(self, case_id: str, *, export_destination: str,
                                  archive_destination: str, close: bool = False) -> dict:
        from foammesh.core.automation import export_archive_recipe
        run = await self.run(
            case_id, export_archive_recipe(export_destination, archive_destination).commands())
        closed = await self.close(case_id) if close and run.result else None
        return {'run': run.to_dict(), 'closed': closed}
