#!/usr/bin/env python
# -*- coding: utf-8 -*-

import copy

import yaml

from foammesh.support import field_complaint
from foammesh.support.simple_db.simple_schema import (
    SimpleSchema, SchemaList, PrimitiveType, EnumType, ValidationError,
    ErrorType, validateData)


def elementToVector(element):
    if 'x' not in element or 'y' not in element or 'z' not in element:
        raise LookupError

    return [float(element['x']), float(element['y']), float(element['z'])]


def elementToList(element, schema, keys):
    if not all(field in element and isinstance(schema[field], PrimitiveType) for field in keys):
        raise LookupError

    return [element[field] for field in keys]


class ConcurrentEditError(RuntimeError):
    """Two working copies changed the same leaf, and neither may be lost.

    A dialog holds its checkout for as long as it is open, so by the time OK
    is clicked another dialog's commit may already have landed. Replacing the
    whole database at that point silently erased the other edit - that is the
    lost update seen live. Independent edits are merged instead; only a
    genuine collision on the same leaf reaches the user, and it names the
    leaves so the message can say what to reopen.
    """

    def __init__(self, paths):
        self.paths = tuple(paths)
        super().__init__(
            'Another change landed first: {0}'.format(', '.join(self.paths)))


def _diffLeaves(schema, baseline, content, prefix=''):
    """Leaf-level changes between two contents of the same schema.

    A list element that appeared or vanished is one leaf, keyed by its own
    path, so adding a volume is a single change rather than a change to every
    field it contains. An element present on both sides is descended into, so
    renaming one and re-typing the other is two independent changes.
    """
    changes = {}

    if isinstance(schema, SchemaList):
        elementSchema = schema.elementSchema()
        for key in content:
            path = f'{prefix}/{key}'
            if key in baseline:
                changes.update(_diffLeaves(
                    elementSchema, baseline[key], content[key], path))
            else:
                changes[path] = ('add', content[key])
        for key in baseline:
            if key not in content:
                changes[f'{prefix}/{key}'] = ('remove', None)
        return changes

    if isinstance(schema, dict):
        for field in schema:
            if field not in content:
                continue
            path = f'{prefix}/{field}' if prefix else field
            if field not in baseline:
                changes[path] = ('set', content[field])
                continue
            changes.update(_diffLeaves(
                schema[field], baseline[field], content[field], path))
        return changes

    if baseline != content:
        changes[prefix] = ('set', content)

    return changes


def _applyLeaf(content, path, change):
    """Write one leaf change into ``content``; ignore it if its parent is gone.

    A change to an element that another commit removed has nowhere to land,
    and re-creating the element from a stale copy would resurrect it.
    """
    kind, value = change
    fields = path.split('/')
    node = content
    for field in fields[:-1]:
        if not isinstance(node, dict) or field not in node:
            return False
        node = node[field]

    if not isinstance(node, dict):
        return False

    last = fields[-1]
    if kind == 'remove':
        node.pop(last, None)
        return True
    if kind == 'set' and last not in node:
        return False

    node[last] = copy.deepcopy(value)
    return True


def _container(content, path):
    """The dict *path* names inside *content*, or ``None`` if it is not there."""
    node = content
    for field in path.split('/'):
        if not isinstance(node, dict) or field not in node:
            return None
        node = node[field]

    return node if isinstance(node, dict) else None


def _referenceLeaves(content, fieldPath, condition):
    """Every ``(element, field)`` the reference pattern *fieldPath* names.

    ``*`` stands for "each element of the list at this level", so
    ``geometry/*/volume`` walks every geometry row. *condition* is checked on
    the element that owns the leaf, which is how one field name can key into
    two different lists depending on what kind of row it sits on.
    """
    fields = fieldPath.split('/')
    nodes = [content]
    for field in fields[:-1]:
        following = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if field == '*':
                following.extend(node.values())
            elif field in node:
                following.append(node[field])
        nodes = following

    leaf = fields[-1]
    for node in nodes:
        if not isinstance(node, dict) or leaf not in node:
            continue
        if condition is not None and str(node.get(condition[0])) != str(condition[1]):
            continue

        yield node, leaf


def _remapReferences(content, references, remap):
    """Rewrite every quoted key in *content* that *remap* renumbered.

    The stored value keeps the type it had: these fields are declared
    ``IntType`` and ``IntType`` validates without coercing, so a value written
    from a widget is the string the widget produced while one written from a
    list key is that key. Rewriting a string as an int would change what
    ``==`` says about it everywhere else.
    """
    for reference in references:
        mapping = remap.get(reference.listPath)
        if not mapping:
            continue

        for element, field in _referenceLeaves(
                content, reference.fieldPath, reference.condition):
            value = element[field]
            if value is None:
                continue
            key = mapping.get(str(value))
            if key is None:
                continue

            element[field] = int(key) if isinstance(value, int) else key


def _keyOrder(key):
    try:
        return 0, int(key), ''
    except (TypeError, ValueError):
        return 1, 0, str(key)


def _getField(pathData):
    schema, content, field = pathData
    return (schema, content) if field is None else (schema[field], content[field])


class Element:
    def __init__(self, element, schema):
        self._data = element
        self._schema = schema

    def value(self, field):
        if isinstance(self._data[field], dict):
            raise LookupError

        return self._data[field]

    def vector(self, field):
        if not isinstance(self._data[field], dict):
            raise LookupError

        return elementToVector(self._data[field])

    def float(self, field):
        return float(self.value(field))

    def int(self, field):
        return int(self.value(field))
    
    def enum(self, field):
        return self._schema[field].toEnum(self.value(field))

    def elements(self, field):
        if not isinstance(self._schema[field], SchemaList):
            raise TypeError

        return {key: Element(self._data[field][key], self._schema[field].elementSchema())
                for key in self._data[field]}

    def element(self, field):
        if not isinstance(self._data[field], dict):
            raise LookupError

        return Element(self._data[field], self._schema[field])


class SimpleDB(SimpleSchema):
    def __init__(self, schema):
        super().__init__(schema)
        self._content = None
        self._editable = False
        self._modified = False
        self._base = ''
        #: Bumped by every commit, so a working copy can tell whether the
        #: database it was taken from has moved on.
        self._revision = 0
        #: What this copy was handed at checkout, kept so a commit can tell
        #: its own edits apart from someone else's.
        self._baseline = None
        self._checkout_revision = None
        #: Elements this copy added that the merge had to renumber, as
        #: ``{list path: {key asked for: key it ended up under}}``. Written by
        #: :meth:`commit` onto the copy that was committed, because that is
        #: what the caller still holds: a dialog that kept the key
        #: ``addElement`` handed it must be able to find its own element
        #: afterwards rather than whatever now occupies that number.
        self._remappedKeys = {}

    def isModified(self):
        return self._modified

    def createData(self):
        self._content = self.generateData()

    def data(self):
        return self._content

    def snapshot(self):
        """The whole document, copied, for an undo stack to hold.

        The engine records one of these before every committed change. It is
        an opaque token to everyone who holds it: it is never written to a
        file, shown to anyone, or compared as text -- it only ever comes back
        to :meth:`restore`. So it is a copy of the content, not a rendering of
        it. MEASURED on the reference configuration: 0.112 ms against the
        4.832 ms the equivalent ``toYaml`` cost, which is 43x, and the dump it
        replaces was about four fifths of the facade's per-command budget.
        """
        return copy.deepcopy(self._content)

    def restore(self, snapshot):
        """Put back a document handed out by :meth:`snapshot`.

        Copied on the way in as well as out, so the same snapshot can be
        restored twice -- redo re-pushes what it just restored -- without the
        second restore handing back a document the first one has since been
        edited through.
        """
        self._content = copy.deepcopy(snapshot)
        self._modified = True

    def checkout(self, path=''):
        """ Creates and returns a SimpleDB replicated with the original's subdata.

        :param path: The root path to clone.
        :return: New SimpleDB based on specific path.
        """
        subSchema = self._schema
        subDB = self._content
        if path != self._base:
            schema, content, field = self._get(path)

            if isinstance(schema, SchemaList):
                subSchema = schema.elementSchema()
            else:
                subSchema = schema[field]

            subDB = content[field]

        subData = self._newDB(subSchema)
        subData._content = copy.deepcopy(subDB)
        subData._baseline = copy.deepcopy(subDB)
        subData._checkout_revision = self._revision
        subData._editable = True
        subData._base = f'{self._base}/{path}' if self._base else path

        return subData

    def idReferences(self):
        """Where this document quotes the keys of its own lists.

        A bare :class:`SimpleDB` knows of none; the project configuration
        answers with the map declared beside its schema. Used only by the
        merge, when an added element has to be renumbered.
        """
        return ()

    def positionAddressedLists(self):
        """Lists whose elements are named by position, not by key.

        A bare :class:`SimpleDB` knows of none; the project configuration
        answers with the paths declared beside its schema. The merge will not
        renumber an addition to one of these, because an index into such a
        list means "the nth element in key order" and moving any element
        changes what every index says. Two copies both adding to one of them
        are editing a sequence rather than adding independent elements, so the
        collision is reported as the conflict it is.
        """
        return ()

    def remappedKeys(self):
        """What the last commit of this copy renumbered, by list path."""
        return {path: dict(mapping)
                for path, mapping in self._remappedKeys.items()}

    def remappedKey(self, path, key):
        """The key an element of *path* ended up under, given the key asked for.

        Answers *key* itself when nothing moved, so a caller can pass every id
        it is holding through here unconditionally.
        """
        mapping = self._remappedKeys.get(path)
        if not mapping:
            return key

        moved = mapping.get(str(key))
        if moved is None:
            return key

        return int(moved) if isinstance(key, int) else moved

    def commit(self, data):
        """ Replaces part of the data

        :param data: SimpleDB based on the root path of the data to be replaced
        :return: what the merge renumbered, as ``remappedKeys`` reports it
        """
        if not data._base.startswith(self._base):
            raise LookupError

        if not data._modified or not data._editable:
            return {}

        remap = {}
        if data._base == self._base:
            if (data._baseline is None
                    or data._checkout_revision == self._revision):
                # Nothing landed since this copy was taken: it is already the
                # whole truth, so replacing is both correct and cheap.
                self._content = data._content
            else:
                remap = self._merge(data)
        else:
            path = data._base[len(self._base) + 1:] if self._base else data._base
            schema, content, field = self._get(path)
            if (data._baseline is None
                    or data._checkout_revision == self._revision):
                # Nothing landed since this copy was taken, so the subtree it
                # holds is the whole truth for that path.
                content[field] = data._content
            else:
                content[field] = self._mergeSubtree(data, path)

        data._remappedKeys = remap
        data._modified = False
        data._editable = False
        self._modified = True
        self._revision += 1

        return remap

    def _mergeSubtree(self, data, path):
        """Apply what a scoped copy changed onto what its subtree holds now.

        DP-460. :class:`ConcurrentEditError` was written for the whole-database
        path and the whole-database path alone: a copy taken of one subtree
        went on replacing that subtree outright, with no revision check of any
        kind, so everything committed under that path while the copy was open
        was erased without a word. MEASURED live on the six multiregion snappy
        legs of 21 September 2026 -- the layer dialog commits a whole-database
        copy holding a new `addLayers/layers` element and the `layerGroup` it
        stamps on each geometry row, and the boundary-layer page then commits
        its `addLayers` subtree. Three of the six came out with the geometry
        stamps intact, because they lie outside that subtree, and
        `addLayersControls { layers { } }` empty, because the group did not.
        snappy then said "No layers to generate ...", finished in a quarter of
        a second, and grew no layers on `jacketed_pipe`, `coaxial_ducts` or
        `shell_and_tube`.

        A collision on the same leaf is reported rather than merged, exactly as
        it is for a whole copy. Two additions that took the same key are
        reported too rather than renumbered: the rekey rewrites every quotation
        of the moved key inside the copy, and a scoped copy cannot see the
        quotations outside it -- `geometry/<id>/layerGroup` names a key of
        `addLayers/layers` from outside `addLayers` -- so moving one here would
        leave those pointing at the other copy's element.
        """
        _, content, field = self._get(path)
        current = content[field]
        changed = {key.lstrip('/'): change for key, change in _diffLeaves(
            data._schema, data._baseline, data._content).items()}
        landed = {key.lstrip('/'): change for key, change in _diffLeaves(
            data._schema, data._baseline, current).items()}

        conflicts = sorted(leaf for leaf in set(changed) & set(landed)
                           if changed[leaf] != landed[leaf])
        if conflicts:
            raise ConcurrentEditError(
                f'{path}/{leaf}' if path else leaf for leaf in conflicts)

        merged = copy.deepcopy(current)
        for leaf in sorted(changed):
            _applyLeaf(merged, leaf, changed[leaf])

        return validateData(merged, data._schema)

    def _merge(self, data):
        """Apply only what this copy changed, onto what is there now."""
        changed = _diffLeaves(self._schema, data._baseline, data._content)
        landed = _diffLeaves(self._schema, data._baseline, self._content)

        # The rekey rewrites the working copy in place, and a conflict found
        # afterwards means the commit never happened. Put the copy back as it
        # was rather than leaving it holding keys it was never told about:
        # its own caller still has the ids ``addElement`` handed it, and only
        # a commit that succeeded reports a move.
        untouched = copy.deepcopy(data._content)
        remap = self._rekeyAdditions(data, changed, landed)
        if remap:
            changed = _diffLeaves(self._schema, data._baseline, data._content)

        conflicts = sorted(path for path in set(changed) & set(landed)
                           if changed[path] != landed[path])
        if conflicts:
            data._content = untouched
            raise ConcurrentEditError(conflicts)

        merged = copy.deepcopy(self._content)
        for path in sorted(changed):
            _applyLeaf(merged, path, changed[path])

        self._content = self.validateData(merged)

        return remap

    def _rekeyAdditions(self, data, changed, landed):
        """Renumber elements this copy added onto keys somebody else took.

        Two working copies that each *add* to the same list both ask the
        allocator for the next key and are both handed it, so the merge then
        sees two different values at one path. That is not a collision of
        intent: nothing was edited twice, two unrelated elements were added
        and the ids happened to be the same number. Refusing the second add
        there blamed the user for their own edit (DP-16).

        The incoming element is moved instead, to a key above every key either
        side holds, so the numeric order existing rows are read in does not
        change. Every quotation of the old key inside this copy is rewritten
        with it -- declared by :meth:`idReferences`, because a referring field
        is an ordinary integer that the schema cannot tell from any other.

        Only this copy's own document is rewritten, and that is what makes the
        rewrite sound: a key it added did not exist at checkout, so every
        quotation of that key anywhere in this copy means the new element and
        nothing else. The database's own rows cannot be quoting it, and the
        element the other copy added under that number keeps both its key and
        its referrers.
        """
        indexed = set(self.positionAddressedLists())
        collided = {}
        for path, change in changed.items():
            if change[0] != 'add':
                continue
            other = landed.get(path)
            if other is None or other[0] != 'add':
                continue
            listPath, _, key = path.rpartition('/')
            if listPath in indexed:
                # An indexed list: renumbering would leave this copy's own
                # blocks and edges pointing at the other copy's vertices,
                # silently. Leave it to the conflict report.
                continue
            collided.setdefault(listPath, []).append(key)

        remap = {}
        for listPath, keys in collided.items():
            listSchema, current = _getField(self._get(listPath))
            if not isinstance(listSchema, SchemaList):
                continue

            incoming = _container(data._content, listPath) or {}
            taken = dict.fromkeys(list(current) + list(incoming))
            mapping = {}
            for key in sorted(keys, key=_keyOrder):
                try:
                    moved = listSchema.key(None, taken)
                except (KeyError, ValidationError, ValueError):
                    # A list whose keys the allocator cannot invent - a
                    # user-named one, say - cannot be renumbered behind the
                    # user's back. Leave it to the conflict report.
                    mapping = {}
                    break
                taken[moved] = None
                mapping[key] = moved

            if mapping:
                remap[listPath] = mapping

        if not remap:
            return {}

        for listPath, mapping in remap.items():
            container = _container(data._content, listPath)
            for old, moved in mapping.items():
                container[moved] = container.pop(old)

        _remapReferences(data._content, self.idReferences(), remap)

        return remap

    def getValue(self, path):
        schema, content, field = self._get(path)
        if isinstance(content[field], dict):
            raise LookupError

        return content[field]

    def getValues(self, path, fields):
        schema, content, field = self._get(path)
        if not isinstance(content[field], dict):
            raise LookupError

        return elementToList(content[field], schema[field], fields)

    def getFloat(self, path):
        value = self.getValue(path)

        return None if value is None else float(value)

    def getVector(self, path):
        schema, content, field = self._get(path)
        if not isinstance(content[field], dict):
            raise LookupError

        return elementToList(content[field], schema[field], ['x', 'y', 'z'])

    def getEnum(self, path):
        schema, content, field = self._get(path)
        if not isinstance(schema[field], EnumType):
            raise LookupError

        return schema[field].toEnum(content[field])

    def setValue(self, path, value, name=None):
        if not self._editable:
            raise LookupError

        schema, content, field = self._get(path)
        value = schema[field].validate(value, name)
        if content[field] != value:
            content[field] = value
            self._modified = True

            return True

        return False

    def setText(self, path, text, name=None):
        if text.strip():
            self.setValue(path, text, name)
            return True

        raise ValidationError(ErrorType.EmptyError, field_complaint.required_clause(), name)

    def newElement(self, path):
        schema, _, = _getField(self._get(path))
        if not isinstance(schema, SchemaList):
            raise TypeError

        db = self._newDB(schema.elementSchema(), True)
        db.createData()

        return db

    def addElement(self, path, newdb, key=None):
        if not self._editable:
            raise LookupError

        schema, content = _getField(self._get(path))

        if not isinstance(schema, SchemaList):
            raise TypeError

        key = schema.key(key, content)
        if key in content:
            raise KeyError

        if schema.elementSchema() == newdb._schema:
            content[key] = schema.validateElement(newdb)
        else:
            raise TypeError

        newdb._editable = False
        self._modified = True

        return key

    def addNewElement(self, path, key=None):
        if not self._editable:
            raise LookupError

        schema, content = _getField(self._get(path))

        if not isinstance(schema, SchemaList):
            raise TypeError

        key = schema.key(key, content)
        if key in content:
            raise KeyError

        element = self._newDB(schema.elementSchema(), True)
        element.createData()
        content[key] = element._content

        self._modified = True

        return key, element

    def getElement(self, path, key=None):
        schema, content, field = self._get(path)
        if key:
            schema, content = _getField((schema, content, field))
            field = key

            if field not in content:
                return None

        if isinstance(schema, SchemaList):
            return Element(content[field], schema.elementSchema())
        else:
            return Element(content[field], schema[field])

    def getElements(self, path: str = None, filter_=None):
        schema, content = _getField(self._get(path))
        if not isinstance(schema, SchemaList):
            raise TypeError

        return {key: Element(content[key], schema.elementSchema())
                for key in content if filter_ is None or filter_(key, content[key])}

    def findElement(self, path=None, filter_=None):
        elements = self.getElements(path, filter_)
        if len(elements) == 1:
            return list(elements.items())[0]

        return None, None

    def getKeys(self, path: str = None, filter_=None):
        schema, content = _getField(self._get(path))
        if not isinstance(schema, SchemaList):
            raise TypeError

        return [key for key in content if filter_ is None or filter_(key, content[key])]

    def removeElement(self, path, key):
        if not self._editable:
            raise LookupError

        schema, content = _getField(self._get(path))

        if not isinstance(schema, SchemaList):
            raise TypeError

        if key not in content:
            raise KeyError

        del content[key]

        self._modified = True

    def removeElements(self, path, keys):
        if not self._editable:
            raise LookupError

        schema, content = _getField(self._get(path))

        if not isinstance(schema, SchemaList):
            raise TypeError

        if any(key not in content for key in keys):
            raise KeyError

        for key in keys:
            del content[key]

        self._modified = True

    def removeElementsByFilter(self, path, function):
        if not self._editable:
            raise LookupError

        schema, content = _getField(self._get(path))

        if not isinstance(schema, SchemaList):
            raise TypeError

        for key in [e[0] for e in content.items() if function(e[0], e[1])]:
            del content[key]

        self._modified = True

    def removeAllElements(self, path):
        if not self._editable:
            raise LookupError

        schema, content, field = self._get(path)

        schema = schema[field]
        if not isinstance(schema, SchemaList):
            raise TypeError

        content[field] = {}

        self._modified = True

    def updateElements(self, path, field, value, filter_=None, name=None):
        schema, content = _getField(self._get(path))
        if not isinstance(schema, SchemaList):
            raise TypeError

        value = schema.elementSchema()[field].validate(value, name)
        keys = [key for key in content if filter_ is None or filter_(key, content[key])]
        for key in keys:
            if content[key][field] != value:
                content[key][field] = value
                self._modified = True

        return keys

    def hasElement(self, path, key):
        schema, content = _getField(self._get(path))

        if not isinstance(schema, SchemaList):
            raise TypeError

        return str(key) in content

    def elementCount(self, path=None, filter_=None):
        if filter_ is not None:
            return len(self.getKeys(path, filter_))

        schema, content = _getField(self._get(path))
        if not isinstance(schema, SchemaList):
            raise TypeError

        return len(content)

    def getUniqueValue(self, path, field, value):
        return f'{value}{self.getUniqueSeq(path, field, value)}'

    def getUniqueSeq(self, path, field, value, start=''):
        if start:
            seq = int(start)
            result = f'{value}{seq}'
        else:
            seq = 0
            result = value

        while self.getElements(path, lambda i, e: e[field] == result):
            seq += 1
            result = f'{value}{seq}'

        return str(seq) if seq or start else ''

    def keyExists(self, path, key):
        schema, content = _getField(self._get(path))
        if not isinstance(schema, SchemaList):
            raise TypeError

        return key in content

    def toYaml(self):
        return yaml.dump(self._content)

    def loadYaml(self, data, fillWithDefault=False):
        self._content = self.validateData(yaml.full_load(data), fillWithDefault=fillWithDefault)

    def _get(self, path):
        if path is None:
            return self._schema, self._content, None

        fields = path.split('/')
        schema = self._schema
        content = self._content

        depth = len(fields) - 1
        for i in range(depth):
            if isinstance(schema, SchemaList):
                schema = schema.elementSchema()
                content = content[fields[i]]
            else:
                schema = schema[fields[i]]
                content = content[fields[i]]

        return schema, content, fields[depth]

    def _newDB(self, schema, editable=False):
        db = SimpleDB(schema)
        db._editable = editable

        return db
