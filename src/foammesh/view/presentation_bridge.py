"""AF4 presentation bridge: facade presentation ops <-> the desktop renderer.

Presentation commands are declarative facade operations (§7). This bridge lets
a GUI submit them through the facade client and applies the resulting
declarative state to a renderer that implements :class:`PresentationRenderer`.
The concrete VTK renderer lives in the view layer; this module is Qt/VTK-free
so the mapping is unit-testable headlessly.
"""
from __future__ import annotations

from typing import Protocol


class PresentationRenderer(Protocol):
    """Minimal renderer surface the bridge drives from facade state."""

    def set_camera_axis(self, axis: str) -> None: ...
    def set_perspective(self, enabled: bool) -> None: ...
    def set_roll(self, degrees: float) -> None: ...
    def set_rotation_center(self, point) -> None: ...
    def fit(self) -> None: ...
    def set_overlay(self, name: str, visible: bool) -> None: ...
    def set_selection(self, entity_ids) -> None: ...
    def set_visibility(self, entity_id: str, visible: bool) -> None: ...


class PresentationBridge:
    """Submit presentation ops via the client and reflect state to a renderer."""

    def __init__(self, client, renderer: PresentationRenderer | None = None):
        self._client = client
        self._renderer = renderer

    def set_renderer(self, renderer: PresentationRenderer) -> None:
        self._renderer = renderer

    async def submit(self, operation: str, parameters: dict | None = None) -> dict:
        result = await self._client.presentation(operation, parameters or {})
        state = result.payload.get('state', {})
        if self._renderer is not None:
            self.reflect(state)
        return state

    def reflect(self, state: dict) -> None:
        """Apply a full declarative presentation state to the renderer."""
        renderer = self._renderer
        if renderer is None:
            return
        renderer.set_camera_axis(state.get('camera_axis', 'iso'))
        renderer.set_perspective(bool(state.get('perspective', True)))
        renderer.set_roll(float(state.get('roll_degrees', 0.0)))
        renderer.set_rotation_center(state.get('rotation_center'))
        for name, visible in state.get('overlays', {}).items():
            renderer.set_overlay(name, bool(visible))
        renderer.set_selection(state.get('selection', []))
        for entity_id, visible in state.get('visibility', {}).items():
            renderer.set_visibility(entity_id, bool(visible))
