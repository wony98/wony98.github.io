"""Reproduce four guards on disposable synthetic mailboxes, using this snapshot."""
from pathlib import Path
from hashlib import sha256
import datetime as dt
import json
import subprocess
import sys
import tempfile

HELPER = Path(__file__).parent / 'skill/scripts/thread_mailbox.py'

def call(thread, *args):
    return subprocess.run([sys.executable, '-B', str(HELPER), '--thread', str(thread), *args],
                          capture_output=True, text=True, encoding='utf-8', timeout=30)

def append_args(last, mode):
    return ['append', '--sender', 'CODEX', '--receiver', 'CLAUDE', '--type', 'REPORT',
            '--tag', 'FIXTURE', '--title', 'Disposable check', '--body', 'fixture only',
            '--reply', 'Y', '--expect-last', str(last), '--expect-mode', mode]

def main():
    results = []
    with tempfile.TemporaryDirectory(prefix='harness-evidence-') as folder:
        thread = Path(folder) / 'fixture.md'
        for args in [('init', '--title', 'Disposable guard test', '--mode', 'LOCAL'), append_args(0, 'LOCAL')]:
            result = call(thread, *args)
            if result.returncode != 0:
                raise RuntimeError('Fixture setup failed: ' + result.stderr)
        cases = [
            ('stale_last_number', append_args(0, 'LOCAL'), 5),
            ('wrong_declared_mode', append_args(1, 'GIT'), 5),
            ('stale_pinned_hash', ['pin', '--expect-mode', 'LOCAL', '--expect-hash', '0'*64, '--body', 'fixture update'], 5),
            ('mixed_line_endings', append_args(1, 'LOCAL'), 2),
        ]
        for name, args, expected in cases:
            if name == 'mixed_line_endings':
                raw = thread.read_bytes()
                if b'\r\n' in raw:
                    raise RuntimeError('Unexpected fixture newline style')
                thread.write_bytes(raw.replace(b'\n', b'\r\n', 1))
            before = sha256(thread.read_bytes()).hexdigest()
            result = call(thread, *args)
            after = sha256(thread.read_bytes()).hexdigest()
            results.append({'case': name, 'expected_exit_code': expected, 'exit_code': result.returncode,
                            'before_sha256': before, 'after_sha256': after, 'unchanged': before == after,
                            'passed': result.returncode == expected and before == after,
                            'message': (result.stderr or result.stdout).strip()})
    report = {'executed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'helper_sha256': sha256(HELPER.read_bytes()).hexdigest(), 'fixture_checks': results,
              'real_threads_modified': False, 'installed_skills_modified': False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(row['passed'] for row in results) else 1

if __name__ == '__main__':
    raise SystemExit(main())
