# Geometry qualification: why a valid mesh can still be refused

`checkMesh` says your mesh is fine. FoamMesh says `qualified: false`. Both are
right, and this page explains why — because the two are answering different
questions.

## The three questions

`checkMesh` asks **is this a valid mesh?** Are the cells convex, are the faces
non-degenerate, is the topology sound. It is the solver's own opinion of
whether it can run, and nothing here overrides it.

It does not ask whether the mesh is a mesh **of your geometry**. A perfectly
valid mesh of the wrong shape passes `checkMesh` comfortably. So FoamMesh asks
two more questions, and reports all three separately:

| Verdict | Question | Answered by |
|---|---|---|
| **Geometry fidelity** | Is the mesh surface where the CAD surface is? | measurement against a validation reference |
| **Resolution adequacy** | Are there enough cells across the features it captured? | traversal of the mesh interior |
| **Mesh quality** | Can the solver run on it? | `checkMesh` |

They are deliberately independent. A mesh can sit exactly on the surface and
still put two cells across a channel that needs ten — fidelity passes,
resolution fails. A mesh can be beautifully regular everywhere and have lost a
cooling fin entirely — quality passes, fidelity fails.

**This is the answer to the question in the title.** `checkMesh` valid,
`qualified: false` means: the solver can run on this mesh, and the mesh is not
of the geometry you asked for. Running it would produce converged, plausible,
well-resolved results for the wrong part.

### A worked example from the test corpus

A finned heat sink, 24 fins, each 1.5 mm thick:

| Mesh | Cells | Fins retained | `checkMesh` |
|---|---|---|---|
| snappy, coarse | 123,940 | **4 of 24** | Mesh OK |
| snappy, fine | 2,363,970 | 24 of 24 | Mesh OK |
| Gmsh | 481,919 | 24 of 24 | Mesh OK |

Every one of those meshes is valid. The first is missing 20 of the 24 surfaces
whose heat transfer is the entire reason the part exists, and no amount of
`checkMesh` will ever say so. Note also that cell count ranks these in the
misleading direction: 124k valid cells retain four fins; 482k retain all
twenty-four.

## What the verdicts mean

Each of the three is one of:

- **`pass`** — measured, and within tolerance.
- **`warning`** — measured, outside the comfortable band but inside the
  failing one.
- **`fail`** — measured, and outside tolerance.
- **`unrated`** — *not* measured. Something needed to measure it was missing:
  no validation reference, no prepared geometry, an identity that could not be
  joined. It is not a pass and it is not a failure; it is the absence of an
  answer.
- **`incomplete`** — measurement started and could not finish inside the
  diagnostic budget. Also not a pass.

**`unrated` and `incomplete` are never green.** This matters more than it
sounds. The tempting shortcut is to treat "we could not check it" as "nothing
wrong found", and that shortcut converts every missing input into a silent
approval. A case with no reference geometry reports `unrated` and
`qualified: false`, and it stays exportable — but as *waived*, not as qualified.

## Tolerance precedence

When more than one tolerance could apply to a surface, the most specific wins:

1. **A tolerance you set on a specific feature or section.** Explicit intent
   outranks everything.
2. **A tolerance you set on the part or region.**
3. **The project default.**
4. **The calibrated threshold.**

A feature you marked critical with a hand-set tolerance keeps that tolerance
when geometry is re-prepared — re-running feature detection will not quietly
demote it to a default.

## Dispositions, and what a waiver is

The summary composes the three verdicts into one disposition:

- **`qualified`** — every required verdict passed. The only green.
- **`waived`** — something did not pass, and an engineer recorded an explicit
  decision to proceed anyway.
- **`report_only`** — qualification is computing and recording but not gating.
  **This is the shipping default** (see below).
- **`unqualified`** — something did not pass and nobody has decided about it.

A **waiver** is a recorded engineering decision, not a dismissal. It requires a
stated reason, it records who made it, and it is bound to the specific mesh it
was made about: re-mesh the case and the waiver stops applying — not because
anything revokes it, but because it no longer describes the mesh in front of
you.

A waiver never produces `qualified: true`. An exported artifact stamped
`waived` is telling whoever receives it exactly that: somebody looked at a
failure and decided to proceed. That remains visible for the life of the
artifact, which is the point.

## Report-only is the default, and why

Geometry qualification ships **dark**. It computes every report, records every
verdict, and blocks nothing.

The reason is honesty about the thresholds. A gate is only as good as the
number it gates on, and those numbers are still being calibrated against a
corpus of known-good and deliberately-broken meshes. Until that corpus shows
that every deliberate defect is caught and no clean mesh is failed, the
thresholds are not promoted — and unpromoted thresholds report `unrated` rather
than pretending to an answer.

You can see what the gate *would* say today. It just will not stop you.

Switching to enforcing mode before thresholds are promoted is refused rather
than silently permitted, because a gate whose numbers nobody has justified is
worse than no gate: it would be believed.

## What to do with a failure

1. **Read which sections failed**, not just the overall verdict. Fidelity is
   reported per named boundary section, so it names the surfaces.
2. **Check whether it is a resolution problem or a fidelity problem.** More
   cells fix resolution. Fidelity failures usually mean refinement is not
   reaching a feature — on snappy, a thin feature below the local cell size is
   the common cause.
3. **Compare engines if the geometry is CAD-backed.** They fail differently:
   snappy recovers surfaces from a background grid and can miss small features;
   Gmsh meshes the CAD volume directly, so a surface is a face of that volume
   and cannot be lost the same way.
4. **Waive only when you have decided the deviation is acceptable** for what
   you are computing, and say so in the reason. The next person to open the
   case reads that sentence.
