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


def _local_api_check() -> dict:
    """Plan 46: the --api-attach host starts, answers, refuses no token, and
    runs an operation (DP-2729: a module the operation reaches lazily was
    missing from the 1.1.3 candidate, and only a call found it).

    The token alone is refused (403) until a client signs the handshake. The
    check signs with a throwaway key, so it needs no Erevnaa private key and
    still proves the packaged build verifies Ed25519 signatures."""
    import asyncio
    import base64
    import urllib.error
    import urllib.request

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from foammesh.core.facade import FoamMeshFacade
    from foammesh.support.client_trust import TRUSTED_CLIENT_KEYS, ClientTrust, signed_message
    from foammesh.support.local_api import LocalApi

    def call(url: str, token: str | None, body: dict | None = None) -> tuple[int, dict | None]:
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request(url, data=data)
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        if token is not None:
            request.add_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b'null')
        except urllib.error.HTTPError as error:
            return error.code, None

    def status(url: str, token: str | None, body: dict | None = None) -> int:
        return call(url, token, body)[0]

    if 'erevnaa-2026-10' not in ClientTrust.erevnaa().keys:
        raise RuntimeError(f'the stock build trusts {sorted(TRUSTED_CLIENT_KEYS)}, '
                           'not the Erevnaa key')
    private = Ed25519PrivateKey.generate()
    trust = ClientTrust({'self-test': base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()})

    def pair(endpoint: str, token: str) -> str | None:
        code, offer = call(f'{endpoint}/api/v1/handshake', token)
        if code != 200 or not offer or not offer.get('required'):
            return None
        signature = base64.b64encode(private.sign(signed_message(offer['challenge'])))
        code, won = call(f'{endpoint}/api/v1/handshake', token, {
            'challenge': offer['challenge'], 'key_id': 'self-test',
            'signature': signature.decode()})
        return won.get('session_token') if code == 200 and won else None

    async def exercise() -> dict:
        with tempfile.TemporaryDirectory(prefix='foammesh-local-api-') as root:
            discovery = Path(root) / 'local-api.json'
            facade = FoamMeshFacade()
            case = facade.create_case(Path(root) / 'case')
            api = LocalApi(facade, discovery_path=discovery,
                           allowed_roots=(Path(root),), client_trust=trust)
            if not await api.start():
                raise RuntimeError(f'local API did not start: {api.problem}')
            try:
                found = json.loads(discovery.read_text(encoding='utf-8'))
                capabilities = f'{found["endpoint"]}/api/v1/system/capabilities'
                health = await asyncio.to_thread(status, f'{found["endpoint"]}/health', None)
                refused = await asyncio.to_thread(status, capabilities, None)
                unsigned = await asyncio.to_thread(status, capabilities, found['token'])
                session = await asyncio.to_thread(pair, found['endpoint'], found['token'])
                allowed = await asyncio.to_thread(status, capabilities, session)
                select = (f'{found["endpoint"]}/api/v1/cases/{case.case_id}'
                          '/operations/mesh.engine.select:execute')
                engine = await asyncio.to_thread(status, select, session, {
                    'parameters': {'engine_id': 'gmsh', 'confirmed': True,
                                   'reason': 'package self-test'}})
            finally:
                await api.stop()
                facade.close_case(case.case_id, save=False)
            if (health, refused, unsigned, allowed, engine) != (200, 401, 403, 200, 200):
                raise RuntimeError(
                    f'health {health}, no token {refused}, token unsigned {unsigned}, '
                    f'signed {allowed}, mesh.engine.select {engine} '
                    '(wanted 200, 401, 403, 200, 200)')
            if discovery.exists():
                raise RuntimeError('the discovery file was left after stop')
            return {'endpoint_host': found['endpoint'].split('//')[1].split(':')[0],
                    'health': health, 'without_token': refused,
                    'token_unsigned': unsigned, 'signed_session': allowed,
                    'mesh_engine_select': engine,
                    'trusted_key_ids': sorted(TRUSTED_CLIENT_KEYS)}

    return asyncio.run(exercise())


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
    _check(checks, 'package.local_api', _local_api_check)

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
