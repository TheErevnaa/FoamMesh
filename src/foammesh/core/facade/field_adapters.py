"""Adapters between semantic field IDs and SimpleDB storage (AF2).

Scalar fields translate directly through their descriptor's ``storage_path``.
Repeated entities (geometry, regions, refinement groups, layer groups) are
addressed by stable references such as ``geometry.items/{id}/cfd_type`` and
edited through collection commands. This module owns value reading, comparison,
and per-element schema validation without importing Qt or a view.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from foammesh.support.simple_db.simple_schema import (
    BoolType, EnumType, FloatType, IntType, PrimitiveType, SchemaList, ValidationError)

from .errors import ValidationFailedError
from .field_metadata import FIELD_OVERRIDES, GROUP_METADATA
from .fields import (CollectionSchema, FieldDescriptor, FieldType, _number, _snake, _title)

_CAMEL = re.compile(r'(?<!^)(?=[A-Z])')

# Curated element-leaf aliases (fidelity to storage, matching §6.2 vocabulary).
ELEMENT_LEAF_ALIASES = {
    'gType': 'geometry_type', 'cfdType': 'cfd_type',
    'nSurfaceLayers': 'surface_layers', 'nonConformal': 'non_conformal',
    'interRegion': 'inter_region',
}


def read_value(configuration: dict, storage_path: str):
    """Read a scalar out of a configuration DTO by ``a/b/c`` storage path."""
    node = configuration
    for part in storage_path.split('/'):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def values_equal(current, new) -> bool:
    """SimpleDB stores every scalar as a trimmed string; compare accordingly."""
    if isinstance(new, bool) or isinstance(current, bool):
        return bool(current) == bool(new)
    return str(current) == str(new)


def coerce_for_apply(descriptor: FieldDescriptor, value):
    """Reject obviously wrong-typed values before the DB validates them."""
    if descriptor.value_type is FieldType.BOOLEAN and not isinstance(value, bool):
        raise ValidationFailedError('field expects a boolean', details={
            'field_id': descriptor.id})
    if descriptor.value_type is FieldType.ENUM and value not in (descriptor.enum or ()):
        raise ValidationFailedError('value is not an allowed enum option', details={
            'field_id': descriptor.id, 'enum': list(descriptor.enum or ())})
    return value


# --------------------------------------------------------------------------- #
# Entity collections
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ElementField:
    relative_id: str          # e.g. 'surface_refinement.minimum_level'
    relative_path: str        # e.g. 'surfaceRefinement/minimumLevel'
    descriptor: FieldDescriptor


def _element_leaf(name: str) -> str:
    return ELEMENT_LEAF_ALIASES.get(name, _snake(name))


def _walk_element(node: dict, prefix_id: str, prefix_path: str, collection_id: str,
                  out: dict, storage_path: str = '') -> None:
    for key, value in node.items():
        rel_path = f'{prefix_path}/{key}' if prefix_path else key
        semantic_leaf = _element_leaf(key)
        rel_id = f'{prefix_id}.{semantic_leaf}' if prefix_id else semantic_leaf
        if isinstance(value, PrimitiveType):
            full_id = f'{collection_id}/{{id}}/{rel_id}'
            descriptor = _build_descriptor_for_element(full_id, rel_path, value, collection_id,
                                                       storage_path)
            out[rel_id] = ElementField(rel_id, rel_path, descriptor)
        elif isinstance(value, SchemaList):
            # Nested repeated entities inside an element are not part of the AF2
            # collection surface; they get their own top-level collection.
            continue
        elif isinstance(value, dict):
            _walk_element(value, rel_id, rel_path, collection_id, out, storage_path)


def _element_unit(storage_path: str, rel_id: str):
    """The collection's declared unit for one element field (DP-606).

    ``GROUP_METADATA`` names units by snake leaf for each storage path, and a
    vector's components (``axis.x``) share the unit of the vector itself.
    """
    units = GROUP_METADATA.get(storage_path, {}).get('units', {}) if storage_path else {}
    return units.get(rel_id, units.get(rel_id.split('.', 1)[0]))


def _build_descriptor_for_element(full_id: str, rel_path: str,
                                  primitive: PrimitiveType, collection_id: str,
                                  storage_path: str = '') -> FieldDescriptor:
    value_type = (FieldType.ENUM if isinstance(primitive, EnumType) else
                  FieldType.BOOLEAN if isinstance(primitive, BoolType) else
                  FieldType.INTEGER if isinstance(primitive, IntType) else
                  FieldType.NUMBER if isinstance(primitive, FloatType) else FieldType.TEXT)
    minimum = maximum = None
    exclusive_min = exclusive_max = False
    enum_options = None
    default = primitive.default()
    if value_type is FieldType.ENUM:
        enum_options = tuple(item.value if isinstance(item.value, str) else item.name
                             for item in primitive._cls)
        default = default if isinstance(default, str) else getattr(default, 'name', default)
    elif value_type in (FieldType.INTEGER, FieldType.NUMBER):
        minimum = _number(primitive._lowLimit) if primitive._lowLimit is not None else None
        maximum = _number(primitive._highLimit) if primitive._highLimit is not None else None
        exclusive_min = minimum is not None and not primitive._lowLimitInclusive
        exclusive_max = maximum is not None and not primitive._highLimitInclusive
        default = _number(default)
    elif value_type is FieldType.BOOLEAN:
        default = bool(default)
    # C31-11. An element field is built here rather than by the registry
    # walk, and until now that meant it could never carry documentation:
    # `FIELD_OVERRIDES` was consulted for scalars only, so a per-row setting
    # rendered with a generated label and an empty tooltip however obscure it
    # was. The override table is keyed by semantic id, and an element's id is
    # exactly the key used there, so the same entries now reach both.
    #
    # DP-606. The unit came from that table alone, so every length in the
    # Gmsh row editors showed bare: the collection's own units, declared in
    # ``GROUP_METADATA`` and already shown on the scalar fields, never reached
    # a row. An override still wins; otherwise the storage path's units do.
    override = FIELD_OVERRIDES.get(full_id, {})
    unit = override.get('unit', _element_unit(storage_path, full_id.split('/')[-1]))
    return FieldDescriptor(
        id=full_id, storage_path=f'{collection_id}/{{id}}/{rel_path}',
        title=override.get('title', _title(full_id.split('/')[-1])),
        value_type=value_type, unit=unit,
        default=default, minimum=minimum, maximum=maximum,
        exclusive_minimum=exclusive_min, exclusive_maximum=exclusive_max,
        enum=enum_options, required=primitive.isRequired(),
        # DP-609. A row setting the run cannot honour is greyed out rather
        # than hidden, with its reason in the documentation.
        read_only=bool(override.get('read_only', False)),
        ui_location=_collection_ui(collection_id),
        documentation=override.get('documentation', ''))


def _collection_ui(collection_id: str) -> str:
    return {
        'geometry.items': 'workflow.geometry',
        'regions.items': 'workflow.region',
        'meshing.castellation.surface_refinements': 'workflow.castellation',
        'meshing.castellation.volume_refinements': 'workflow.castellation',
        'meshing.castellation.feature_bands': 'workflow.castellation',
        'meshing.castellation.volume_bands': 'workflow.castellation',  # DP-586
        # Plan 37 UF16. Read at castellation, authored beside the seeds.
        'meshing.castellation.exclude_points': 'workflow.region',
        'meshing.layers.groups': 'workflow.layers',
        'geometry.interface_pairs': 'workflow.geometry.interfaces',
    }.get(collection_id, '')


class EntityAdapter:
    """Element-field descriptors and validation for one collection."""

    def __init__(self, collection: CollectionSchema):
        self.collection = collection
        self.fields: dict[str, ElementField] = {}
        _walk_element(collection.element_schema, '', '', collection.collection_id, self.fields,
                      collection.storage_path)

    @property
    def collection_id(self) -> str:
        return self.collection.collection_id

    @property
    def storage_path(self) -> str:
        return self.collection.storage_path

    def descriptor(self, relative_id: str) -> FieldDescriptor:
        try:
            return self.fields[relative_id].descriptor
        except KeyError as error:
            raise ValidationFailedError('unknown entity field', details={
                'collection': self.collection_id, 'field': relative_id}) from error

    def relative_path(self, relative_id: str) -> str:
        return self.fields[relative_id].relative_path if relative_id in self.fields else None

    def normalize_patch(self, patch: dict) -> dict:
        """Validate an element patch against the element schema."""
        if not isinstance(patch, dict) or not patch:
            raise ValidationFailedError('entity patch must be a non-empty object')
        unknown = sorted(set(patch) - set(self.fields))
        if unknown:
            raise ValidationFailedError('unknown entity field', details={
                'collection': self.collection_id, 'fields': unknown})
        normalized = {}
        for relative_id, value in patch.items():
            element_field = self.fields[relative_id]
            coerce_for_apply(element_field.descriptor, value)
            primitive = self._primitive(element_field.relative_path)
            try:
                primitive.validate(value, relative_id)
            except ValidationError as error:
                raise ValidationFailedError('entity value failed validation', details={
                    'collection': self.collection_id, 'field': relative_id,
                    'error': error.message}) from error
            normalized[element_field.relative_path] = value
        return normalized

    def _primitive(self, relative_path: str) -> PrimitiveType:
        node = self.collection.element_schema
        for part in relative_path.split('/'):
            node = node[part]
        return node


def build_entity_adapters(collections: dict[str, CollectionSchema]) -> dict[str, EntityAdapter]:
    return {collection_id: EntityAdapter(collection)
            for collection_id, collection in collections.items()}
