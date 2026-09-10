"""AST-based enforcement for facade adapter boundaries (PC0).

Regex gates missed aliased imports and legacy global access.  This scanner
reports stable ``rule + relative file`` findings; line/symbol data remains
diagnostic so allowlists do not depend on source line churn.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


ADAPTER_ROOTS = (
    'src/foammesh/view', 'src/widgets', 'src/foammesh/api',
    'src/foammesh/cli', 'src/foammesh/agent',
)
PATH_MUTATORS = {'write_text', 'write_bytes', 'unlink', 'mkdir', 'rmdir', 'touch'}
MODULE_FILESYSTEM_MUTATORS = {
    'os.remove', 'os.replace', 'os.rename', 'shutil.copy', 'shutil.copy2',
    'shutil.copytree', 'shutil.move', 'shutil.rmtree',
}
PROCESS_SYMBOLS = {'Popen', 'run', 'call', 'check_call', 'check_output', 'QProcess',
                   'RunUtility', 'RunParallelUtility'}
LEGACY_DB_MUTATORS = {
    'commit', 'setValue', 'addElement', 'removeElement', 'updateElement',
    'updateElements', 'newElement', 'save', 'saveAs',
}
LEGACY_PROJECT_MUTATORS = {
    'save', 'saveAs', 'saveStateAsCase', 'saveStateCopy',
    'markArtifactChanged', 'markGeometryChanged', 'setParallelEnvironment',
}
LEGACY_JOB_MUTATORS = {'start', 'cancel', 'cancel_all', 'submit'}


@dataclass(frozen=True, order=True)
class BoundaryViolation:
    rule: str
    path: str
    line: int
    symbol: str

    @property
    def key(self) -> tuple[str, str]:
        return self.rule, self.path

    def to_dict(self) -> dict:
        return {'rule': self.rule, 'path': self.path, 'line': self.line,
                'symbol': self.symbol}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return '.'.join(reversed(parts))


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.violations: set[BoundaryViolation] = set()
        self.module_aliases: dict[str, str] = {}
        self.symbol_aliases: dict[str, str] = {}

    def add(self, rule: str, node: ast.AST, symbol: str) -> None:
        self.violations.add(BoundaryViolation(rule, self.path,
                                              getattr(node, 'lineno', 0), symbol))

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            alias = item.asname or item.name.split('.')[0]
            self.module_aliases[alias] = item.name
            if item.name == 'subprocess':
                self.add('process_launch', node, item.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ''
        for item in node.names:
            alias = item.asname or item.name
            qualified = f'{module}.{item.name}' if module else item.name
            self.symbol_aliases[alias] = qualified
            # Importing the desktop application handle is not itself a domain
            # boundary violation: converted pages use ``app.facadeClient`` and
            # presentation-only shell objects. Attribute scanning below flags
            # only the forbidden legacy state/service members.
            if module.startswith('foammesh.openfoam'):
                self.add('openfoam_import', node, qualified)
            if item.name in PROCESS_SYMBOLS:
                self.add('process_launch', node, qualified)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = _dotted(node)
        root, _, rest = dotted.partition('.')
        resolved = self.module_aliases.get(root, root)
        qualified = f'{resolved}.{rest}' if rest else resolved
        if qualified.startswith('subprocess.'):
            self.add('process_launch', node, qualified)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        symbol = _dotted(node.func)
        leaf = symbol.rsplit('.', 1)[-1]
        resolved = self.symbol_aliases.get(symbol, symbol)
        root, _, rest = symbol.partition('.')
        module = self.module_aliases.get(root, root)
        qualified = f'{module}.{rest}' if rest else resolved
        if symbol.startswith('app.state.'):
            self.add('legacy_app_access', node, symbol)
        elif symbol.startswith('app.db.') and leaf in LEGACY_DB_MUTATORS:
            self.add('legacy_app_access', node, symbol)
        elif symbol.startswith('app.project.') and leaf in LEGACY_PROJECT_MUTATORS:
            self.add('legacy_app_access', node, symbol)
        elif symbol.startswith('app.jobManager.') and leaf in LEGACY_JOB_MUTATORS:
            self.add('legacy_app_access', node, symbol)
        if (resolved.rsplit('.', 1)[-1] in PROCESS_SYMBOLS
                and resolved != symbol) or qualified.startswith('subprocess.'):
            self.add('process_launch', node, resolved)
        if leaf in PATH_MUTATORS or qualified in MODULE_FILESYSTEM_MUTATORS:
            self.add('filesystem_mutation', node, symbol)
        if leaf == 'open':
            mode_node = node.args[1] if len(node.args) > 1 else next(
                (item.value for item in node.keywords if item.arg == 'mode'), None)
            if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
                if any(marker in mode_node.value for marker in ('w', 'a', 'x', '+')):
                    self.add('filesystem_mutation', node, symbol)
        self.generic_visit(node)


def scan_adapter_boundaries(root: str | Path) -> tuple[BoundaryViolation, ...]:
    root = Path(root).resolve()
    violations: set[BoundaryViolation] = set()
    for relative_root in ADAPTER_ROOTS:
        directory = root / relative_root
        if not directory.is_dir():
            continue
        for path in directory.rglob('*.py'):
            if path.name.endswith('_ui.py') or '__pycache__' in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'), filename=relative)
            except SyntaxError as error:
                violations.add(BoundaryViolation(
                    'syntax_error', relative, error.lineno or 0, str(error)))
                continue
            scanner = _Scanner(relative)
            scanner.visit(tree)
            violations.update(scanner.violations)
    return tuple(sorted(violations))


def load_boundary_allowlist(path: str | Path) -> dict[tuple[str, str], dict]:
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    entries = document.get('entries')
    defaults = document.get('defaults', {})
    if document.get('schema_version') != 1 or not isinstance(entries, list):
        raise ValueError('unsupported PC0 boundary allowlist')
    result = {}
    for raw_entry in entries:
        entry = {**defaults.get(raw_entry.get('rule'), {}), **raw_entry}
        required = {'rule', 'path', 'owner', 'rationale', 'removal_package', 'expires'}
        if not required <= set(entry):
            raise ValueError(f'incomplete PC0 boundary allowlist entry: {entry!r}')
        date.fromisoformat(entry['expires'])
        key = entry['rule'], entry['path']
        if key in result:
            raise ValueError(f'duplicate PC0 boundary allowlist entry: {key!r}')
        result[key] = entry
    return result


def boundary_delta(root: str | Path, allowlist_path: str | Path) -> dict:
    violations = scan_adapter_boundaries(root)
    current = {item.key for item in violations}
    allowed = load_boundary_allowlist(allowlist_path)
    return {
        'unapproved': [item.to_dict() for item in violations if item.key not in allowed],
        'stale': [allowed[key] for key in sorted(set(allowed) - current)],
        'active': [allowed[key] for key in sorted(set(allowed) & current)],
    }
