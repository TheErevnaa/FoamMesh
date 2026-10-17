#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The quality report a user can keep (Plan 39 VI17, I7).

*Mesh quality > Export report...* writes an HTML page and a Markdown page next
to the PNG captures of the case: the metrics table, the histogram, the worst
cells with their centroids, what the check recorded, the identity of the run
and a capture of the view. It is the file the testing engineer attaches to a
comment.

Nothing here measures anything. It lays out what the Mesh quality tab shows,
row for row, so the report lists the same worst cells as the tab. No Qt: the
view hands over the rows it shows and the PNG bytes it drew.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path

from foammesh.core.capture import captures_dir, ensure_captures_dir, new_stamp
from foammesh.core.quantities import aligned

HISTOGRAM_NAME = 'quality-histogram'
VIEW_NAME = 'quality-view'
REPORT_PREFIX = 'quality-report'

#: Identity keys in the order the report prints them (key, label).
IDENTITY_ROWS = (
    ('engine', 'Engine'), ('stage', 'Stage'), ('revision', 'Revision'),
    ('cells', 'Cells'), ('case', 'Case'), ('created', 'Created'),
)
METRIC_COLUMNS = (
    ('measure', 'Metric'), ('requestedMinimum', 'Limit'),
    ('achievedMinimum', 'Worst'), ('achievedMean', 'Mean'),
    ('belowThreshold', 'Below'), ('verdict', 'Verdict'),
)


def _text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:.6g}'
    return str(value)


def _centroid(element: dict) -> str:
    centroid = element.get('centroid') or ()
    # DP-165: a coordinate triple is one measurement, aligned to one precision.
    return ', '.join(aligned(centroid))


def worst_rows(elements) -> list:
    """``[(tag, value, centroid text, zone)]`` for the elements listed."""
    return [(_text(e.get('tag')), _text(e.get('value')), _centroid(e),
             _text(e.get('zone'))) for e in (elements or ())]


def metric_rows(metrics) -> list:
    return [[_text(m.get(key)) for key, _label in METRIC_COLUMNS]
            for m in (metrics or ())]


def _table_html(headers, rows) -> str:
    head = ''.join(f'<th>{escape(h)}</th>' for h in headers)
    body = ''.join(
        '<tr>' + ''.join(f'<td>{escape(c)}</td>' for c in row) + '</tr>'
        for row in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _table_md(headers, rows) -> str:
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['| ' + ' | '.join(cell(h) for h in headers) + ' |',
             '|' + '|'.join(' --- ' for _ in headers) + '|']
    lines += ['| ' + ' | '.join(cell(c) for c in row) + ' |' for row in rows]
    return '\n'.join(lines)


def _findings(blocking, advisory) -> list:
    return ([('Blocking', str(item)) for item in (blocking or ())]
            + [('Advisory', str(item)) for item in (advisory or ())])


def build_report(*, identity: dict, verdict: dict, metric_label: str,
                 worst_title: str, worst: list, limit=None,
                 histogram_image: str = '', view_image: str = '',
                 histogram: dict | None = None) -> tuple:
    """``(html, markdown)`` for the data the Mesh quality tab shows."""
    identity_rows = [(label, _text(identity.get(key)))
                     for key, label in IDENTITY_ROWS if identity.get(key)]
    metrics = metric_rows(verdict.get('metrics'))
    metric_headers = [label for _key, label in METRIC_COLUMNS]
    found = _findings(verdict.get('blocking'), verdict.get('advisory'))
    worst_headers = ['Element', 'Value', 'Centroid', 'Zone']
    worst_table = worst_rows(worst)
    headline = _text(verdict.get('reason') or verdict.get('verdict'))
    total = (histogram or {}).get('total')
    limit_text = _text(limit) if limit is not None else 'none'

    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<title>Mesh quality report</title>',
         '<style>body{font-family:sans-serif;margin:2em;max-width:60em}'
         'table{border-collapse:collapse;margin:.5em 0}'
         'td,th{border:1px solid #999;padding:.2em .6em;text-align:left}'
         'img{max-width:100%}</style></head><body>',
         '<h1>Mesh quality report</h1>']
    m = ['# Mesh quality report', '']
    if headline:
        h.append(f'<p>{escape(headline)}</p>')
        m += [headline, '']
    h.append('<h2>Run</h2>' + _table_html(['', ''], identity_rows))
    m += ['## Run', '', _table_md(['Item', 'Value'], identity_rows), '']
    h.append('<h2>Metrics</h2>')
    m += ['## Metrics', '']
    if metrics:
        h.append(_table_html(metric_headers, metrics))
        m += [_table_md(metric_headers, metrics), '']
    else:
        h.append('<p>This mesh has not been checked yet.</p>')
        m += ['This mesh has not been checked yet.', '']
    if found:
        h.append('<h2>Check summary</h2><ul>' + ''.join(
            f'<li><b>{escape(kind)}:</b> {escape(text)}</li>'
            for kind, text in found) + '</ul>')
        m += ['## Check summary', ''] + [
            f'- **{kind}:** {text}' for kind, text in found] + ['']
    h.append(f'<h2>Distribution: {escape(metric_label)}</h2>')
    m += [f'## Distribution: {metric_label}', '']
    h.append(f'<p>Limit: {escape(limit_text)}'
             + (f'; {total} cells' if total else '') + '.</p>')
    m += [f'Limit: {limit_text}' + (f'; {total} cells' if total else '') + '.',
          '']
    if histogram_image:
        h.append(f'<img src="{escape(histogram_image)}" alt="Histogram of '
                 f'{escape(metric_label)}">')
        m += [f'![Histogram of {metric_label}]({histogram_image})', '']
    h.append(f'<h2>{escape(worst_title)}</h2>')
    m += [f'## {worst_title}', '']
    if worst_table:
        h.append(_table_html(worst_headers, worst_table))
        m += [_table_md(worst_headers, worst_table), '']
    else:
        h.append('<p>No cells are listed.</p>')
        m += ['No cells are listed.', '']
    if view_image:
        h.append('<h2>View</h2>'
                 f'<img src="{escape(view_image)}" alt="The view when the '
                 'report was written">')
        m += ['## View', '', f'![The view]({view_image})', '']
    h.append('</body></html>')
    return '\n'.join(h), '\n'.join(m)


def write_report(case_root, *, identity: dict, verdict: dict,
                 metric_label: str, worst_title: str, worst: list,
                 limit=None, histogram: dict | None = None,
                 histogram_png: bytes | None = None,
                 view_png: bytes | None = None,
                 stamp: str | None = None) -> dict:
    """Write the report beside the case's captures; returns the paths."""
    ensure_captures_dir(case_root)
    directory = captures_dir(case_root)
    stamp = stamp or new_stamp()
    names = {}
    if histogram_png:
        names['histogram'] = f'{REPORT_PREFIX}-{stamp}-{HISTOGRAM_NAME}.png'
        (directory / names['histogram']).write_bytes(histogram_png)
    if view_png:
        names['view'] = f'{REPORT_PREFIX}-{stamp}-{VIEW_NAME}.png'
        (directory / names['view']).write_bytes(view_png)
    identity = dict(identity)
    identity.setdefault('created', datetime.now(timezone.utc).isoformat(
        timespec='seconds'))
    html, markdown = build_report(
        identity=identity, verdict=verdict or {}, metric_label=metric_label,
        worst_title=worst_title, worst=worst, limit=limit,
        histogram=histogram, histogram_image=names.get('histogram', ''),
        view_image=names.get('view', ''))
    base = directory / f'{REPORT_PREFIX}-{stamp}'
    html_path = base.with_suffix('.html')
    md_path = base.with_suffix('.md')
    html_path.write_text(html, encoding='utf-8')
    md_path.write_text(markdown, encoding='utf-8')
    return {'html': str(html_path), 'markdown': str(md_path),
            'images': [str(directory / n) for n in names.values()]}
