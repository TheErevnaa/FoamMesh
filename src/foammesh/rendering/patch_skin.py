"""Plan 39 VI4 (F1e): the patches are edges only over the volume's exterior.

With a full volume on screen, the region's ``internalMesh`` exterior, every
boundary patch and (for an enclosure) the fade all describe the same faces,
and each used to be drawn whole, with its edges: twice the faces, twice the
lines, and a depth fight settled only by polygon offset. The exterior now
carries ``patchId`` (the patch each face belongs to, see
``mesh_preview.exterior_from_patches``), is coloured by it, and a patch in
its plain state draws only its edges over it.

A patch is in its plain state when it is shown, opaque, not selected, not
painted by a field, not moved, not cut and drawn as a surface or surface with
edges. In any other state the patch draws itself exactly as it always did and
the exterior shows its neutral colour under it, so every cue a patch has
(hide, fade, selection, deviation colour, explode, section) is the cue it
had. The whole skin stands down while the volume itself is hidden, faded,
selected, cut, filtered or painted (Colour mesh by).

``PatchSkin`` owns that decision for one region: it listens to the volume and
its patches, and when any of them changes how it looks it works out which
patches are plain, colours the exterior from them and tells each patch
whether to draw its faces. A change of one patch is looked at alone (a
snappy case has hundreds of them, and a mode set on all of them is that many
changes, not that many squared).
"""
from __future__ import annotations

#: Switched off by a test that wants the doubled drawing, and by nothing else.
ENABLED = True


class PatchSkin:
    """One region's volume and the patches that are its skin.

    ``patches`` are the region's boundary actors in ``patchId`` order (the
    order the worker numbered them: the region's boundary order).
    """

    def __init__(self, volume, patches: list):
        self._volume = volume
        self._patches = list(patches)
        self._wanted = False
        self._plain = [False] * len(self._patches)
        self._colours: list = [None] * len(self._patches)
        self._plainCount = 0
        self._neutral = None
        self._busy = False
        self._disposed = False
        self._links: list = []
        self._link(volume, self.refresh)
        for index, patch in enumerate(self._patches):
            self._link(patch, lambda *_ignored, i=index: self._refreshOne(i))
        self.refresh()

    def _link(self, actor, slot) -> None:
        actor.appearanceChanged.connect(slot)
        actor.colorChanged.connect(slot)
        self._links.append((actor, slot))

    # -- what is asked of it ------------------------------------------------ #

    def volume(self):
        return self._volume

    def patches(self) -> list:
        return list(self._patches)

    def active(self) -> bool:
        """Whether the exterior is carrying the patches' colours now."""
        return self._wanted

    def skinned(self) -> list:
        """The patches that are drawing their edges only."""
        return [p for p in self._patches if p.isSkinned()]

    def patchAt(self, index: int):
        """The patch an exterior face of ``patchId`` *index* shows, if it is
        drawn by the exterior now (otherwise the face is the volume's)."""
        if not self._wanted or not 0 <= index < len(self._patches):
            return None
        patch = self._patches[index]
        return patch if patch.isSkinned() else None

    # -- the decision -------------------------------------------------------- #

    def refresh(self, *_ignored) -> None:
        """The volume changed (or everything did): look at all of it."""
        if self._busy or self._disposed:
            return
        self._busy = True
        try:
            self._refreshAll()
        finally:
            self._busy = False

    def _refreshAll(self) -> None:
        volume = self._volume
        wanted = bool(ENABLED and volume.canCarrySkin(len(self._patches)))
        plain = [bool(wanted and p.isPlain()) for p in self._patches]
        if not any(plain):
            wanted = False
            plain = [False] * len(self._patches)
        neutral = volume.neutralSkinColour() if wanted else None
        colours = [
            (p.skinColour() if flag else neutral) if wanted else None
            for p, flag in zip(self._patches, plain)]
        changed = (wanted != self._wanted or plain != self._plain
                   or colours != self._colours or neutral != self._neutral
                   or volume.skinApplied() != wanted)
        if not changed:
            return
        if wanted and not volume.applySkin(colours):
            wanted = False
            plain = [False] * len(self._patches)
            colours = [None] * len(self._patches)
        if not wanted:
            volume.dropSkin()
        self._wanted, self._plain, self._colours = wanted, plain, colours
        self._neutral = neutral if wanted else None
        self._plainCount = sum(plain)
        for patch, flag in zip(self._patches, plain):
            patch.setSkinned(flag)

    def _refreshOne(self, index: int) -> None:
        """One patch changed: only its own entry can have."""
        if self._busy or self._disposed:
            return
        if not self._wanted:
            # Nothing is skinned; only a patch becoming plain can start it,
            # and that is the whole region's question.
            if ENABLED and self._patches[index].isPlain():
                self.refresh()
            return
        volume = self._volume
        if volume.skinApplied() is False:
            # The volume's picture was reset under us (a cut came in).
            self.refresh()
            return
        patch = self._patches[index]
        plain = bool(patch.isPlain())
        colour = patch.skinColour() if plain else self._neutral
        if plain == self._plain[index] and colour == self._colours[index]:
            return
        self._busy = True
        try:
            self._plainCount += int(plain) - int(self._plain[index])
            self._plain[index] = plain
            self._colours[index] = colour
            if self._plainCount <= 0:
                self._refreshAll()
                return
            volume.setSkinColour(index, colour)
            patch.setSkinned(plain)
        finally:
            self._busy = False

    def dispose(self) -> None:
        """Take the skin off and let go of the actors."""
        if self._disposed:
            return
        self._disposed = True
        for actor, slot in self._links:
            for signal in (actor.appearanceChanged, actor.colorChanged):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._links = []
        if not self._volume.isDisposed():
            self._volume.dropSkin()
        for patch in self._patches:
            if not patch.isDisposed():
                patch.setSkinned(False)
        self._wanted = False
        self._plain = [False] * len(self._patches)
        self._colours = [None] * len(self._patches)
        self._plainCount = 0
