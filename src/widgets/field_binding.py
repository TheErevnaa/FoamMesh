"""AF4 reusable field bindings: Qt widgets <-> facade field IDs.

A :class:`PageBinder` declares a page's ``field_id -> widget`` map. It reads the
current values from the facade (``load``), collects a batch patch from the
widgets (``collect``), and submits one ``configuration.patch`` command through
the :class:`DesktopFacadeClient` (``apply``). All engineering writes therefore
go through the facade command dispatcher — the page holds no direct
``ProjectState``/``Configurations`` write path.

The binder is deliberately widget-duck-typed (``text()``/``setText()``,
``isChecked()``/``setChecked()``, ``currentData()``/``value()``) so it works
with plain Qt widgets and with fakes in headless tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from foammesh.core.facade import FIELD_REGISTRY, FieldType


def _widget_get(widget):
    """Read a value from a Qt-like widget, mirroring FoamMesh page conventions."""
    if hasattr(widget, 'isChecked'):
        return widget.isChecked()
    if hasattr(widget, 'currentData'):
        data = widget.currentData()
        if data is not None:
            return data
    if hasattr(widget, 'text'):
        return widget.text()
    if hasattr(widget, 'value'):
        return widget.value()
    raise TypeError(f'unsupported widget for binding: {type(widget)!r}')


def _widget_set(widget, value) -> None:
    if hasattr(widget, 'setChecked') and isinstance(value, bool):
        widget.setChecked(bool(value))
    elif hasattr(widget, 'setCurrentData'):
        widget.setCurrentData(value)
    elif hasattr(widget, 'findData') and hasattr(widget, 'setCurrentIndex'):
        index = widget.findData(value)
        if index < 0:
            raise ValueError(f'widget has no item for bound value: {value!r}')
        widget.setCurrentIndex(index)
    elif hasattr(widget, 'setText'):
        widget.setText('' if value is None else str(value))
    elif hasattr(widget, 'setValue'):
        widget.setValue(value)
    else:
        raise TypeError(f'unsupported widget for binding: {type(widget)!r}')


def _coerce(descriptor, raw):
    if descriptor.value_type is FieldType.BOOLEAN:
        return bool(raw)
    if raw is None or raw == '':
        return None if not descriptor.required else raw
    return raw


@dataclass
class FieldBinding:
    field_id: str
    widget: object

    def descriptor(self):
        return FIELD_REGISTRY.get(self.field_id)

    def read(self):
        return _coerce(self.descriptor(), _widget_get(self.widget))

    def write(self, value) -> None:
        _widget_set(self.widget, value)


@dataclass
class PageBinder:
    """Binds a page's widgets to facade fields and applies edits as one command."""
    client: object
    owner_id: str
    bindings: list = field(default_factory=list)
    _lease: object = None

    def bind(self, field_id: str, widget) -> 'PageBinder':
        # Fail fast if a page references an unknown field id.
        FIELD_REGISTRY.get(field_id)
        self.bindings.append(FieldBinding(field_id, widget))
        return self

    def field_ids(self) -> list:
        return [binding.field_id for binding in self.bindings]

    def load(self) -> None:
        """Populate every bound widget from the current facade snapshot."""
        values = self.client.field_values(self.field_ids())
        for binding in self.bindings:
            binding.write(values[binding.field_id])

    def collect(self) -> dict:
        """Build the batch patch from the current widget values."""
        return {binding.field_id: binding.read() for binding in self.bindings}

    def claim(self) -> None:
        """Acquire a dirty-form field claim so external edits get a conflict.

        The claim is owned by the local human (the client actor), so this form's
        own apply is allowed while a concurrent agent/REST edit is refused.
        """
        self._lease = self.client.claim(self.field_ids())

    def release(self) -> None:
        claim_id = getattr(self._lease, 'claim_id', None)
        self.client.release(claim_id)
        self._lease = None

    async def apply(self, *, expected_revision: int | None = None):
        """Submit the collected patch as a single human GUI command."""
        result = await self.client.apply_fields(self.collect(),
                                                 expected_revision=expected_revision)
        self.release()
        return result

    def has_external_change(self, base_revision: int) -> bool:
        """True when the facade authored revision moved past the form's base."""
        return self.client.revisions()['authored_revision'] != base_revision
