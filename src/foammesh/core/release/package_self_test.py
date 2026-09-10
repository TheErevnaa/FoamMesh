"""Self-contained qualification checks executed by the frozen desktop binary."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _check(checks: list[dict], check_id: str, operation) -> None:
    """Run one check and retain bounded, machine-readable failure evidence."""
    try:
        details = operation()
        checks.append({
            'check_id': check_id,
            'status': 'pass',
            'details': details if isinstance(details, dict) else {'value': details},
        })
    except Exception as error:
        checks.append({
            'check_id': check_id,
            'status': 'fail',
            'details': {
                'exception': type(error).__name__,
                'message': str(error)[:2000],
            },
        })


def _fresh_case_check() -> dict:
    from foammesh.core.engine import ENGINE_REGISTRY, configured_engine_id
    from foammesh.core.facade import FoamMeshFacade
    from foammesh.db.configurations_schema import CURRENT_CONFIGURATIONS_VERSION

    with tempfile.TemporaryDirectory(prefix='foammesh-package-self-test-') as temporary:
        case_path = Path(temporary) / 'fresh-case'
        facade = FoamMeshFacade()
        session = facade.create_case(case_path)
        try:
            configuration = session.configuration()
            version = int(configuration.get('version'))
            engine = configured_engine_id(session.state.db)
            registry = ENGINE_REGISTRY.ids()
            store_exists = (case_path / 'configurations.h5').is_file()
            if version != CURRENT_CONFIGURATIONS_VERSION:
                raise RuntimeError(
                    f'fresh configuration version is {version!r}, '
                    f'expected {CURRENT_CONFIGURATIONS_VERSION}')
            if engine != 'unselected':
                raise RuntimeError(f'fresh-case engine must be unselected, got {engine!r}')
            if 'snappy' not in registry:
                raise RuntimeError(f'unexpected engine registry: {registry!r}')
            if not store_exists:
                raise RuntimeError('fresh configuration store was not persisted')
            return {
                'configuration_version': version,
                'default_engine': engine,
                'registered_engines': list(registry),
                'configuration_store': 'configurations.h5',
            }
        finally:
            facade.close_case(session.case_id)


def _no_migration_check() -> dict:
    forbidden = (
        'foammesh.db.migrations',
        'foammesh.core.case.migrations',
        'foammesh.core.case.migration',
    )
    present = [name for name in forbidden if importlib.util.find_spec(name) is not None]
    if present:
        raise RuntimeError('migration namespaces are packaged: ' + ', '.join(present))
    return {'forbidden_namespaces_absent': list(forbidden)}


def _resource_check() -> dict:
    from resources import resource
    from foammesh.core.branding import BrandingManifest
    from foammesh.core.documentation import document_path

    legal = {}
    for name in ('LICENSE', 'NOTICE', 'THIRD_PARTY.md'):
        path = document_path(name)
        if path is None:
            raise RuntimeError(f'packaged legal document is missing: {name}')
        legal[name] = str(path)
    guide_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[4]))
    guide = guide_root / 'docs' / 'user_manual' / 'getting_started.md'
    if not guide.is_file():
        raise RuntimeError('packaged getting-started guide is missing')
    manifest = BrandingManifest.load(resource.file('branding/manifest.json'))
    assets = manifest.validate_files()
    return {
        'legal_documents': legal,
        'getting_started': str(guide),
        'validated_branding_assets': [path.name for path in assets],
    }


def _openfoam_check() -> dict:
    from foammesh.core.shell import CapabilityRegistry

    diagnostics = CapabilityRegistry().runtime_diagnostics()
    selected = diagnostics.get('selected_profile')
    if not selected:
        reasons = [
            item.get('reason', '') for item in diagnostics.get('profiles', ())]
        raise RuntimeError(
            'OpenFOAM Foundation 13 profile is unavailable: ' +
            '; '.join(filter(None, reasons)))
    if selected.get('project') != 'OpenFOAM' or selected.get('version') != '13':
        raise RuntimeError(
            'runtime identity is not OpenFOAM Foundation 13: '
            f'{selected.get("project")} {selected.get("version")}')
    if diagnostics.get('native_fallback_allowed'):
        raise RuntimeError('Windows package unexpectedly permits a native runtime fallback')
    return {
        'required_project': diagnostics['required_project'],
        'required_version': diagnostics['required_version'],
        'profile_id': selected['profile_id'],
        'project': selected['project'],
        'version': selected['version'],
        'wm_options': selected['wm_options'],
        'utility_count': len(selected.get('utilities', {})),
        'mpi_identity': selected.get('mpi_identity', ''),
        'runtime_fingerprint': selected.get('fingerprint', ''),
    }


def run(report_path: str | Path) -> dict:
    """Execute frozen-package gates and always persist their complete result."""
    destination = Path(report_path).resolve()
    checks: list[dict] = []
    _check(checks, 'package.frozen_runtime', lambda: {
        'frozen': bool(getattr(sys, 'frozen', False)),
        'executable': str(Path(sys.executable).resolve()),
        'payload_root': str(Path(getattr(sys, '_MEIPASS', '')).resolve())
        if hasattr(sys, '_MEIPASS') else None,
    } if getattr(sys, 'frozen', False) else (_ for _ in ()).throw(
        RuntimeError('self-test is not running from a frozen package')))
    _check(checks, 'package.schema_fresh', _fresh_case_check)
    _check(checks, 'package.no_migrations', _no_migration_check)
    _check(checks, 'package.resources', _resource_check)
    _check(checks, 'runtime.openfoam_foundation_13', _openfoam_check)

    failed = [item['check_id'] for item in checks if item['status'] != 'pass']
    document = {
        'schema_version': 1,
        'captured_at': _utc_now(),
        'checks': checks,
        'summary': {
            'passed': len(checks) - len(failed),
            'failed': len(failed),
            'failed_checks': failed,
            'all_passed': not failed,
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    return document
