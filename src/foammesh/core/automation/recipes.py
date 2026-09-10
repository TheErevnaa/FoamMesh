"""AF6 deterministic recipes (§10).

FoamMesh provides deterministic operations and recipes, not LLM reasoning. A
recipe is an ordered list of typed facade commands (the same generic command
envelope a plan uses). The external chatbot decides *what* to propose; a recipe
is how a proposed workflow is expressed so the facade can validate, confirm, and
execute it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecipeStep:
    operation: str
    parameters: dict = field(default_factory=dict)
    checkpoint: bool = False   # snapshot/quality gate after this step

    def as_command(self) -> dict:
        return {'operation': self.operation, 'parameters': dict(self.parameters)}


@dataclass
class Recipe:
    name: str
    steps: list[RecipeStep]

    def commands(self) -> list[dict]:
        return [step.as_command() for step in self.steps]

    def to_dict(self) -> dict:
        return {'name': self.name,
                'steps': [{'operation': s.operation, 'parameters': s.parameters,
                           'checkpoint': s.checkpoint} for s in self.steps]}


def configure_and_check_recipe(patch: dict) -> Recipe:
    """A reversible-edit + QA recipe: apply refinement settings, then check mesh."""
    return Recipe('configure_and_check', [
        RecipeStep('configuration.patch', {'patch': patch}),
        RecipeStep('mesh.check', {}, checkpoint=True),
    ])


def remesh_recipe(patch: dict) -> Recipe:
    """The canonical meshing recipe (§10): configure -> stages -> inspect/check."""
    return Recipe('remesh', [
        RecipeStep('configuration.patch', {'patch': patch}),
        RecipeStep('workflow.run_stage', {'stage': 'blockMesh'}),
        RecipeStep('workflow.run_stage', {'stage': 'snappyHexMesh'}, checkpoint=True),
        RecipeStep('mesh.info', {}),
        RecipeStep('mesh.check', {}, checkpoint=True),
    ])


def geometry_import_recipe(source: str) -> Recipe:
    return Recipe('geometry_import_diagnostics', [
        RecipeStep('geometry.import', {'source': source}, checkpoint=True),
        RecipeStep('geometry.diagnostics', {}, checkpoint=True),
    ])


def authored_mesh_recipe(patch: dict) -> Recipe:
    return Recipe('authored_mesh', [
        RecipeStep('configuration.patch', {'patch': patch}),
        RecipeStep('workflow.generate_dictionaries', {}, checkpoint=True),
        RecipeStep('workflow.run_stage', {'stage': 'blockMesh'}, checkpoint=True),
        RecipeStep('workflow.run_stage', {'stage': 'snappyHexMesh'}, checkpoint=True),
        RecipeStep('mesh.info', {}),
        RecipeStep('mesh.check', {}, checkpoint=True),
    ])


def quality_assessment_recipe() -> Recipe:
    """Read-only half of the separately confirmed QA adjustment journey."""
    return Recipe('quality_assessment', [RecipeStep('quality.report', {}, checkpoint=True)])


def quality_adjustment_recipe(patch: dict) -> Recipe:
    """A new authorization is required after assessment before adjustment/remesh."""
    return Recipe('quality_adjustment_remesh', [
        RecipeStep('configuration.patch', {'patch': patch}),
        RecipeStep('workflow.generate_dictionaries', {}),
        RecipeStep('workflow.run_stage', {'stage': 'snappyHexMesh'}, checkpoint=True),
        RecipeStep('mesh.check', {}, checkpoint=True),
    ])


def transform_recovery_recipe(kind: str, parameters: dict) -> Recipe:
    if kind not in {'scale', 'translate', 'rotate'}:
        raise ValueError(f'unsupported transform: {kind}')
    return Recipe('transform_with_recovery', [
        RecipeStep(f'mesh.transform.{kind}', dict(parameters), checkpoint=True),
        RecipeStep('mesh.info', {}),
    ])


def converter_import_recipe(source: str, format_: str) -> Recipe:
    return Recipe('converter_import_with_rollback', [
        RecipeStep('mesh.import.converter', {'source': source, 'format': format_}, checkpoint=True),
        RecipeStep('mesh.info', {}),
        RecipeStep('mesh.check', {}, checkpoint=True),
    ])


def export_archive_recipe(export_destination: str, archive_destination: str) -> Recipe:
    return Recipe('export_save_archive', [
        RecipeStep('case.export.native', {'destination': export_destination}, checkpoint=True),
        RecipeStep('case.save', {}),
        RecipeStep('case.archive', {'destination': archive_destination}, checkpoint=True),
    ])
