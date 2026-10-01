#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
from pathlib import Path

import yaml

from foammesh.support.simple_db.simple_db import SimpleDB

from .configurations_schema import (
    CONFIGURATIONS_VERSION_KEY,
    CURRENT_CONFIGURATIONS_VERSION,
    ID_REFERENCES,
    POSITION_ADDRESSED_LISTS,
    migrateDocument,
    schema,
)
from .file_db import (writeConfigurations, readConfigurations, readSavedSeq,
                      FileGroup, newFiles)


FILE_NAME = 'configurations.h5'
DB_KEY = 'configurations'

#: Why each configuration version was retired, keyed by the version that could
#: no longer be opened.  A bare "unsupported version" told the user nothing
#: about what to do next.
_VERSION_NOTES = {
    13: ('the SALOME hybrid meshing pipeline was removed and replaced by Gmsh; '
         'projects saved on it have no engine to open with'),
}


class ConfigurationVersionError(ValueError):
    """A project file was written by an incompatible FoamMesh version."""

    def __init__(self, found: int, expected: int):
        self.found = found
        self.expected = expected
        reason = _VERSION_NOTES.get(found)
        message = (f'this project uses FoamMesh configuration version {found}, '
                   f'but this build reads version {expected}')
        if reason:
            message += f'. Version {found} was retired because {reason}'
        message += ('. Re-import the geometry into a new project to continue.'
                    if found < expected else
                    '. Upgrade FoamMesh to open it.')
        super().__init__(message)


class Configurations(SimpleDB):
    _geometryNextKey = 0

    def __init__(self, schema):
        super().__init__(schema)

        self._path = None
        self._files = newFiles()

        self._defaults = None

        #: Plan 35 CR9. ``seq`` of the last change set the file on disk holds,
        #: and the project's autosave journal once a session attaches one.
        self.savedSeq = 0
        self.autosave = None

    def create(self, path):
        self._path = path / FILE_NAME
        self.createData()
        self._save()

    def load(self, path):
        self._path = path / FILE_NAME
        data, files, maxIds = readConfigurations(self._path)
        document = yaml.full_load(data)
        version = int(document.get(CONFIGURATIONS_VERSION_KEY, 0))
        if version != CURRENT_CONFIGURATIONS_VERSION:
            raise ConfigurationVersionError(version, CURRENT_CONFIGURATIONS_VERSION)
        # Fill absent leaves from the schema instead of refusing the file: a
        # field added within the same configuration version must not make an
        # existing project unopenable. Values that are present are still
        # validated, so out-of-range or wrong-enum data is rejected as before.
        # A *renamed* field has to be rewritten before that, because
        # validation keeps only the keys the schema names and would otherwise
        # throw the old spelling away and substitute a default.
        self._content = self.validateData(
            migrateDocument(document), fillWithDefault=True)
        self._files = files
        Configurations._geometryNextKey = maxIds[FileGroup.GEOMETRY_POLY_DATA.value]
        self.savedSeq = readSavedSeq(self._path)

    def save(self):
        # A change set that left the document as it was still has a seq the
        # file must cover, or it would be offered back after a crash.
        autosave = self._ownAutosave()
        if self.isModified() or (
                autosave is not None and autosave.applied_seq > self.savedSeq):
            self._save()

    def saveAs(self, path):
        self._path = path / FILE_NAME
        self._save()

    def addGeometryPolyData(self, pd):
        Configurations._geometryNextKey += 1
        key = f'Geometry{Configurations._geometryNextKey}'

        self._files['geometry'][key] = pd
        self._modified = True

        return key

    def removeGeometryPolyData(self, key):
        # Plan 37 UF20. A primitive's surfaces have no file (``path`` is
        # None); removing one used to store a ``None`` key beside the string
        # ones, and every later sort of the store -- the copy an unlock keeps
        # for its undo -- failed with "'<' not supported between NoneType
        # and str".
        if key is None:
            return
        self._files['geometry'][key] = None

    def geometryPolyData(self, key):
        return self._files['geometry'][key]

    def updateGeometryPolyData(self, key, pd):
        self._files['geometry'][key] = pd
        self._modified = True

    def checkout(self, path: str = ''):
        """Clone the data *and* the file store.

        ``SimpleDB.checkout`` copies only the document, so a working copy used
        to start with no files at all: ``checkout().geometryPolyData(key)``
        raised ``KeyError`` for every surface that already existed. That is the
        read the 3D scene makes for each imported geometry, so nothing an
        import produced could ever be drawn, and the domain bounds derived from
        those actors stayed unknown. The copy is shallow on purpose -- the VTK
        objects are shared, and :meth:`commit` merges the mapping back.
        """
        working = super().checkout(path)
        for group, entries in self._files.items():
            working._files.setdefault(group, {}).update(entries)
        return working

    def idReferences(self):
        """The project schema's own map of where list keys are quoted.

        See ``ID_REFERENCES``. The merge reads this when it has to renumber an
        element two working copies both added at the same key.
        """
        return ID_REFERENCES

    def positionAddressedLists(self):
        """The project schema's lists that are indexed rather than keyed.

        See ``POSITION_ADDRESSED_LISTS``. The merge reads this to know which
        collections it must not renumber, because moving an element of one
        changes what every index into it means.
        """
        return POSITION_ADDRESSED_LISTS

    def commit(self, data):
        for key in data._files:
            self._files[key].update(data._files[key])

        return super().commit(data)

    def _newDB(self, schema, editable=False):
        db = Configurations(schema)
        db._editable = editable

        return db

    def _ownAutosave(self):
        """The autosave journal of the project this db is saved into, if any."""
        autosave = self.autosave
        if autosave is None or self._path is None:
            return None
        try:
            if Path(self._path).resolve().parent != autosave.directory:
                return None  # a copy saved elsewhere does not settle our journal
        except OSError:
            return None
        return autosave

    def _save(self):
        """Replace the configuration store only after a complete new file exists.

        A partially written HDF5 file makes an in-place OpenFOAM case impossible
        to reopen.  Keeping the previous file until ``os.replace`` succeeds is
        the minimum recovery guarantee for all FoamMesh-owned case state.

        Plan 35 CR9 save order: take ``seq`` = N *before* serialising (so the
        content holds at least everything through N), write the temporary,
        fsync, replace, fsync the directory, and only then tell the journal it
        may compact through N. A crash anywhere before the replace leaves the
        old file and its old ``saved_seq``; the journal still covers the rest.
        """
        from foammesh.core.project.journal import fault_point, fsync_directory
        autosave = self._ownAutosave()
        savedSeq = autosave.applied_seq if autosave is not None else self.savedSeq
        temporary = self._path.with_suffix(f'{self._path.suffix}.tmp')
        try:
            writeConfigurations(temporary, self.toYaml(), self._files, savedSeq)
            fault_point('save-temp-written', file=FILE_NAME)
            # Windows only permits fsync on a writable descriptor.
            with temporary.open('r+b') as saved:
                os.fsync(saved.fileno())
            fault_point('save-temp-fsynced', file=FILE_NAME)
            os.replace(temporary, self._path)
            fault_point('save-replaced', file=FILE_NAME)
        finally:
            if temporary.exists():
                temporary.unlink()
        try:
            fsync_directory(self._path.parent)
        except OSError:
            pass
        fault_point('save-dir-fsynced', file=FILE_NAME)
        self._modified = False
        if autosave is not None:
            self.savedSeq = savedSeq
            autosave.saved(savedSeq)


defaultsDB = Configurations(schema)
defaultsDB.createData()
