"""One self-contained HTML document about a mesh, including where it fell short.

Plan 26 WP12/WP8. ``core/quality`` already produced machine-readable report
artefacts -- ``FidelityReport``, ``QualityReport``, and ``summary.compose``,
which binds fidelity, resolution and mesh quality into ``summary.json``. None
of them is a *document*. There was no HTML, PDF or Markdown generation anywhere
in ``core/quality`` or ``core/export``, and ``quality.report.export`` is
hard-restricted to ``.json``/``.csv``. Nothing composed layers, the run
manifest, the prepared-revision chain and the renderer into one artefact a
human could read or send.

Three properties are the reason this is a module rather than an f-string.

**It composes; it never recomputes.** Every figure here was measured by
something else and is quoted. A report that recalculated would eventually
disagree with the gate that made the decision, and the disagreement would
surface as an argument about which number was right.

**A mesh that failed says so first.** The acceptance criterion is explicit: a
mesh that failed ``checkMesh`` must produce a report that says so prominently
and must never look like a pass. So the verdict is the first thing in the
document, before the counts that make a bad mesh look impressive, and a
missing section is rendered as *not measured* rather than omitted -- an absent
section reads as "nothing to report", which is the opposite of what it means.

**It is self-contained.** Inline CSS, no external requests, no web fonts. A
report that needs a network to render is one that stops rendering the moment
it is sent anywhere, which is exactly when it is needed.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from foammesh.core.quality.layer_report import coverage_rows

REPORT_SCHEMA_VERSION = 1
#: Where the document lands by default, beside the machine-readable reports.
DEFAULT_REPORT_PATH = 'foammesh/quality/mesh-report.html'

#: Verdict -> (label, css class). Every one carries a word; colour is
#: reinforcement, never the sole carrier -- the same rule as the verdict strip.
_VERDICT = {
    'pass': ('PASS', 'pass'),
    'blemish': ('PASS WITH BLEMISH', 'warn'),
    'warning': ('WARNING', 'warn'),
    'fail': ('FAIL', 'fail'),
    'invalid': ('INVALID', 'fail'),
    'waived': ('WAIVED — accepted by a recorded decision', 'warn'),
    'unrated': ('NOT RATED', 'muted'),
    'incomplete': ('INCOMPLETE', 'muted'),
}

_STYLE = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 0 auto; max-width: 62rem; padding: 2rem 1.5rem 4rem;
       background: #fff; color: #1a1a1a; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2.2rem 0 .6rem;
     border-bottom: 1px solid #d8d8d8; padding-bottom: .3rem; }
.sub { color: #5a5a5a; margin: 0 0 1.4rem; }
.verdict { border: 2px solid; border-radius: .4rem; padding: .9rem 1.1rem;
           margin: 0 0 1.6rem; font-size: 1.05rem; }
.verdict .word { font-weight: 700; letter-spacing: .04em; }
.verdict.pass  { border-color: #2e7d32; background: #f0f8f1; }
.verdict.warn  { border-color: #b26a00; background: #fdf6ec; }
.verdict.fail  { border-color: #c62828; background: #fdf0f0; }
.verdict.muted { border-color: #8a8a8a; background: #f5f5f5; }
table { border-collapse: collapse; width: 100%; margin: .4rem 0 1rem;
        font-size: .93rem; }
th, td { text-align: left; padding: .38rem .6rem;
         border-bottom: 1px solid #e6e6e6; vertical-align: top; }
th { background: #f4f4f4; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.tag { display: inline-block; padding: .05rem .45rem; border-radius: .2rem;
       font-size: .82rem; font-weight: 600; }
.tag.pass { background: #e3f2e4; color: #1b5e20; }
.tag.warn { background: #fbeeda; color: #8a4b00; }
.tag.fail { background: #fbe0e0; color: #a01818; }
.tag.muted{ background: #ececec; color: #4a4a4a; }
.missing { color: #6a6a6a; font-style: italic; }
figure { margin: .6rem 0; }
figure img { max-width: 100%; border: 1px solid #ddd; border-radius: .3rem; }
footer { margin-top: 3rem; color: #6a6a6a; font-size: .85rem;
         border-top: 1px solid #e0e0e0; padding-top: .8rem; }
@media (prefers-color-scheme: dark) {
  body { background: #141414; color: #e8e8e8; }
  h2 { border-color: #3a3a3a; }
  th { background: #232323; } th, td { border-color: #303030; }
  .sub, .missing, footer { color: #a0a0a0; }
  .verdict.pass  { background: #14230f; }
  .verdict.warn  { background: #241c0d; }
  .verdict.fail  { background: #2a1414; }
  .verdict.muted { background: #1f1f1f; }
  .tag.pass { background: #1b3a1d; color: #b7e2ba; }
  .tag.warn { background: #3a2c10; color: #f0cd8c; }
  .tag.fail { background: #3a1717; color: #f3b2b2; }
  .tag.muted{ background: #2a2a2a; color: #c0c0c0; }
}
@media print { body { max-width: none; } .verdict { border-width: 1px; } }
"""


def _e(value) -> str:
    return html.escape('' if value is None else str(value))


def _number(value, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _e(value)
    if number == int(number) and abs(number) < 1e15:
        return f'{int(number):,}'
    return f'{number:.{digits}g}'


def _rows(pairs) -> str:
    """A two-column table. **Both** columns are escaped.

    They were not: the value was interpolated raw, so a case named
    ``<script>...`` reached the document intact. Every value here comes from a
    path, a filename or a configuration string -- all user-supplied -- and a
    report is a document people send to each other.
    """
    body = ''.join(
        f'<tr><th>{_e(name)}</th><td>{_e(value)}</td></tr>'
        for name, value in pairs if value not in (None, ''))
    return f'<table>{body}</table>' if body else _missing()


def _missing(reason: str = 'Not measured for this mesh.') -> str:
    """Say a section was not measured rather than omitting it.

    An omitted section reads as "nothing to report", which is the opposite of
    what an unmeasured one means.
    """
    return f'<p class="missing">{_e(reason)}</p>'


def _verdict_block(verdict: str, detail: str) -> str:
    label, style = _VERDICT.get(str(verdict or '').lower(),
                                (str(verdict).upper() or 'UNKNOWN', 'muted'))
    return (f'<div class="verdict {style}"><span class="word">{_e(label)}'
            f'</span>{f" &mdash; {_e(detail)}" if detail else ""}</div>')


def _tag(verdict: str) -> str:
    label, style = _VERDICT.get(str(verdict or '').lower(),
                                (str(verdict).upper() or '-', 'muted'))
    return f'<span class="tag {style}">{_e(label)}</span>'


class MeshReport:
    """Everything a reader needs about one mesh, composed from what exists."""

    def __init__(self, document: dict):
        self.document = dict(document or {})

    # -- sections ---------------------------------------------------------- #

    def header(self) -> str:
        header = self.document.get('header') or {}
        return _rows((
            ('Case', header.get('case')),
            ('Generated', header.get('generated')),
            ('Engine', header.get('engine')),
            ('Runtime fingerprint', header.get('runtime_fingerprint')),
            ('Configuration revision', header.get('configuration_revision')),
        ))

    def geometry(self) -> str:
        geometry = self.document.get('geometry') or {}
        if not geometry:
            return _missing('No prepared geometry was recorded.')
        bbox = geometry.get('bbox') or ()
        summary = _rows((
            ('Sources', ', '.join(geometry.get('sources') or ()) or None),
            ('Unit as declared', geometry.get('declared_unit')),
            ('Unit as interpreted', geometry.get('interpreted_unit')),
            ('Bounding box', ' , '.join(_number(v) for v in bbox) or None),
            ('Prepared revision', geometry.get('prepared_revision')),
        ))
        patches = geometry.get('patches') or ()
        if not patches:
            return summary
        rows = ''.join(
            f'<tr><td>{_e(item.get("name"))}</td>'
            f'<td>{_e(item.get("category") or item.get("type"))}</td></tr>'
            for item in patches)
        return (summary + '<table><thead><tr><th>Patch</th><th>Category</th>'
                f'</tr></thead><tbody>{rows}</tbody></table>')

    def repair_history(self) -> str:
        history = self.document.get('repair_history') or ()
        if not history:
            return _missing('No repair was applied to this geometry.')
        rows = ''.join(
            f'<tr><td class="num">{_e(item.get("revision"))}</td>'
            f'<td>{_e(item.get("kind"))}</td>'
            f'<td>{_e(item.get("action") or "-")}</td>'
            f'<td>{_e(item.get("fingerprint"))}</td></tr>' for item in history)
        return ('<table><thead><tr><th>Revision</th><th>Kind</th>'
                '<th>Action</th><th>Fingerprint</th></tr></thead>'
                f'<tbody>{rows}</tbody></table>')

    def settings(self) -> str:
        settings = self.document.get('settings') or {}
        if not settings:
            return _missing('No run manifest was found for this mesh.')
        # Values are escaped by `_rows`; anything pre-escaped here would be
        # escaped twice and render its own entities.
        return _rows(sorted(
            (name, json.dumps(value) if isinstance(value, (dict, list))
             else _number(value) if isinstance(value, (int, float))
             else value)
            for name, value in settings.items()))

    def result(self) -> str:
        result = self.document.get('result') or {}
        if not result:
            return _missing('No mesh has been produced.')
        counts = _rows((
            ('Cells', _number(result.get('cells'))),
            ('Faces', _number(result.get('faces'))),
            ('Points', _number(result.get('points'))),
            ('Meshing time', result.get('seconds') and
             f'{_number(result["seconds"])} s'),
        ))
        by_type = result.get('cells_by_type') or {}
        if not by_type:
            return counts
        rows = ''.join(
            f'<tr><td>{_e(name)}</td><td class="num">{_number(count)}</td></tr>'
            for name, count in sorted(by_type.items()))
        return (counts + '<table><thead><tr><th>Cell type</th><th>Count</th>'
                f'</tr></thead><tbody>{rows}</tbody></table>')

    def quality(self) -> str:
        quality = self.document.get('quality') or {}
        checks = quality.get('checks') or ()
        if not checks:
            return _missing('checkMesh has not been run on this mesh.')
        rows = ''.join(
            f'<tr><td>{_e(item.get("name"))}</td>'
            f'<td class="num">{_number(item.get("value"))}</td>'
            f'<td>{_tag(item.get("verdict"))}</td>'
            f'<td>{_e(item.get("detail") or "")}</td></tr>' for item in checks)
        table = ('<table><thead><tr><th>Check</th><th>Value</th>'
                 '<th>Verdict</th><th>Detail</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table>')
        sets = quality.get('sets') or ()
        if sets:
            written = ''.join(
                f'<tr><td>{_e(item.get("name"))}</td>'
                f'<td class="num">{_number(item.get("count"))}</td></tr>'
                for item in sets)
            table += ('<table><thead><tr><th>Set written</th><th>Entities</th>'
                      f'</tr></thead><tbody>{written}</tbody></table>')
        return table

    #: Row verdict -> the tag colour that reinforces it. The word is the
    #: carrier; the colour never is.
    _LAYER_TAG = {'complete': 'pass', 'partial': 'warn',
                  'not grown': 'fail', 'frozen': 'muted'}

    def layers(self) -> str:
        """Requested against achieved, per patch, with both units named.

        Plan 32 check 5. This table existed and was right, but it did not say
        which of its right-hand numbers was a length and which was a share,
        and it kept its own opinion of what `complete` meant. Both now come
        from :func:`core.quality.layer_report.coverage_rows`, which the
        Quality page reads as well -- a document and a page that disagree
        about the same run is the failure this module opens by refusing.
        """
        rows = coverage_rows(self.document.get('layers') or {})
        if not rows:
            return _missing(
                'No boundary layers were requested, or the layer stage has '
                'not run.')
        body = ''
        for row in rows:
            verdict = row['verdict']
            body += (
                f'<tr><td>{_e(row["patch"])}</td>'
                f'<td class="num">{_e(row["requested_text"])}</td>'
                f'<td class="num">{_e(row["achieved_text"])}</td>'
                f'<td class="num">{_e(row["thickness_text"])}</td>'
                f'<td class="num">{_e(row["coverage_text"])}</td>'
                f'<td><span class="tag '
                f'{self._LAYER_TAG.get(verdict, "muted")}">'
                f'{_e(verdict)}</span></td></tr>')
        return ('<table><thead><tr><th>Patch</th><th>Requested</th>'
                '<th>Achieved</th><th>Overall thickness</th>'
                '<th>Of requested thickness</th><th></th></tr></thead>'
                f'<tbody>{body}</tbody></table>')

    def warnings(self) -> str:
        warnings = self.document.get('warnings') or ()
        if not warnings:
            return ('<p class="missing">Nothing was changed silently: no '
                    'clamp, drop or substitution was recorded.</p>')
        rows = ''
        for item in warnings:
            if not isinstance(item, dict):
                rows += f'<tr><td colspan="3">{_e(item)}</td></tr>'
                continue
            substitution = ''
            if item.get('requested') is not None or item.get('applied') is not None:
                substitution = (f'{_e(item.get("requested"))} &rarr; '
                                f'{_e(item.get("applied"))}')
            rows += (f'<tr><td>{_e(item.get("message"))}</td>'
                     f'<td>{_e(item.get("field_id") or "")}</td>'
                     f'<td>{substitution}</td></tr>')
        return ('<table><thead><tr><th>Warning</th><th>Field</th>'
                '<th>Requested &rarr; applied</th></tr></thead>'
                f'<tbody>{rows}</tbody></table>')

    def views(self) -> str:
        views = self.document.get('views') or ()
        if not views:
            return ''
        figures = ''.join(
            f'<figure><img alt="{_e(item.get("caption") or "mesh view")}" '
            f'src="{_e(item.get("data_uri"))}">'
            f'<figcaption>{_e(item.get("caption") or "")}</figcaption>'
            '</figure>' for item in views if item.get('data_uri'))
        return f'<h2>Views</h2>{figures}' if figures else ''

    # -- document ---------------------------------------------------------- #

    def to_html(self) -> str:
        header = self.document.get('header') or {}
        verdict = str(self.document.get('verdict') or 'unrated')
        title = f'Mesh report — {header.get("case") or "case"}'
        sections = (
            ('Header', self.header()),
            ('Geometry', self.geometry()),
            ('Repair history', self.repair_history()),
            ('Settings', self.settings()),
            ('Result', self.result()),
            ('Quality', self.quality()),
            ('Layers', self.layers()),
            ('Warnings', self.warnings()),
        )
        body = ''.join(f'<h2>{_e(name)}</h2>{content}'
                       for name, content in sections)
        return (
            '<!doctype html>\n<html lang="en">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f'<title>{_e(title)}</title>\n<style>{_STYLE}</style>\n'
            '</head>\n<body>\n'
            f'<h1>{_e(title)}</h1>\n'
            f'<p class="sub">{_e(header.get("generated") or "")}</p>\n'
            # The verdict comes first, before any count that could make a bad
            # mesh look impressive.
            + _verdict_block(verdict, str(self.document.get('reason') or ''))
            + body + self.views()
            + '<footer>Generated by FoamMesh. Every figure in this report was '
              'measured by the stage that produced it and is quoted here, not '
              'recalculated.</footer>\n'
            '</body>\n</html>\n')

    def write(self, destination) -> Path:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(self.to_html(), encoding='utf-8', newline='\n')
        os.replace(temporary, path)
        return path


def compose(*, header=None, geometry=None, repair_history=(), settings=None,
            result=None, quality=None, layers=None, warnings=(), views=(),
            verdict='unrated', reason='') -> MeshReport:
    """Assemble the report document. Composition only -- nothing is measured here."""
    return MeshReport({
        'schema_version': REPORT_SCHEMA_VERSION,
        'header': dict(header or {}),
        'geometry': dict(geometry or {}),
        'repair_history': list(repair_history or ()),
        'settings': dict(settings or {}),
        'result': dict(result or {}),
        'quality': dict(quality or {}),
        'layers': dict(layers or {}),
        'warnings': list(warnings or ()),
        'views': list(views or ()),
        'verdict': verdict,
        'reason': reason,
    })


def worst_verdict(*verdicts) -> str:
    """The governing verdict across sections, worst wins.

    A document is only as good as its worst section: reporting the best one
    would produce exactly the pass-looking report the acceptance criterion
    forbids.
    """
    order = ('pass', 'blemish', 'warning', 'waived', 'unrated', 'incomplete',
             'fail', 'invalid')
    ranked = [str(item or '').lower() for item in verdicts if item]
    known = [item for item in ranked if item in order]
    if not known:
        return 'unrated'
    return max(known, key=order.index)
