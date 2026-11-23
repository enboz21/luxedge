"""Read packaged Python bytecode; never execute the backend or installer."""
import hashlib
import marshal
import types
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]


def same_code(a, b):
    if isinstance(a, types.CodeType) and isinstance(b, types.CodeType):
        return (a.co_code == b.co_code and a.co_names == b.co_names
                and a.co_varnames == b.co_varnames
                and len(a.co_consts) == len(b.co_consts)
                and all(same_code(x, y) for x, y in zip(a.co_consts, b.co_consts)))
    return a == b


def verify():
    binary = ROOT / 'lush_backend.exe'
    paths = [binary, ROOT / 'dist/backend/lush_backend.exe',
             ROOT / 'dist/win-unpacked/resources/lush_backend.exe']
    extracted = ROOT / 'build/installer-verification/resources/lush_backend.exe'
    if extracted.exists():
        paths.append(extracted)
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    assert len(set(hashes)) == 1, 'Backend binaries differ'
    archive = CArchiveReader(str(binary))
    modules = archive.open_embedded_archive('PYZ.pyz')
    embedded = (
        'frame_processing', 'monitor_catalog', 'connectivity', 'device_overview',
        'diagnostic_logs', 'stream_diagnostics', 'wemos_diagnostics',
        'windows_wfp', 'windows_audit_session', 'windows_audit_helper',
    )
    code_by_name = {'ambilight_pc': marshal.loads(archive.extract('ambilight_pc'))}
    code_by_name.update({name: modules.extract(name) for name in embedded})
    for name, code in code_by_name.items():
        source = (ROOT / f'{name}.py').read_text(encoding='utf-8')
        assert same_code(code, compile(source, f'{name}.py', 'exec')), f'Stale code: {name}'
    print('Backend bytecode matches current source; SHA256:', hashes[0])


if __name__ == '__main__':
    verify()
