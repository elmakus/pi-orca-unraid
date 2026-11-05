"""CI diagnostic reporting must be precise and must never leak captured output."""
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load('smoke_for_tests', 'runtime-smoke.py')
report = load('report_for_tests', 'ci-failure-summary.py')


class SmokeDiagnosticsTests(unittest.TestCase):
    def test_failed_subprocess_never_reproduces_sensitive_output(self):
        secret = 'private-token-123456789'
        with patch.object(smoke.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['docker', 'exec'], 37, secret, secret)):
            with self.assertRaises(smoke.SmokeFailure) as failure:
                smoke.run(['docker', 'exec'])
        self.assertEqual(str(failure.exception), 'subprocess exited with status 37; output withheld')
        self.assertNotIn(secret, str(failure.exception))

    def test_timeout_never_reproduces_sensitive_output(self):
        with patch.object(smoke.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                ['docker', 'exec', 'SECRET'], 17, output=b'PRIVATE')):
            with self.assertRaises(smoke.SmokeFailure) as failure:
                smoke.run(['docker', 'exec'], timeout=17)
        self.assertEqual(str(failure.exception), 'subprocess timed out after 17s; output withheld')

    def test_failure_file_contains_only_sanitized_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audit.json'
            smoke.record_failure(path, 'wait for PTY child ready marker',
                                 'long-lived PTY child did not start', 'SmokeFailure')
            data = path.read_text()
            self.assertIn('wait for PTY child ready marker', data)
            self.assertNotIn('token', data)
            self.assertNotIn('password', data)

    def test_summary_identifies_failing_step_and_specific_smoke_condition(self):
        text = report.make_summary({'RUNTIME_SMOKE':'failure','DOCKER_BUILD':'success'},
                    {'result':'failure','stage':'wait for PTY child ready marker',
                     'category':'SmokeFailure','reason':'long-lived PTY child did not start'})
        self.assertIn('Run isolated runtime smoke test', text)
        self.assertIn('wait for PTY child ready marker', text)
        self.assertIn('long-lived PTY child did not start', text)

    def test_malicious_diagnostic_text_is_not_emitted(self):
        secret = '${{ secrets.GITHUB_TOKEN }}'
        text = report.make_summary({'RUNTIME_SMOKE':'failure'},
                    {'result':'failure','stage':secret,'reason':'secret\nmore','category':'SmokeFailure'})
        self.assertNotIn(secret, text)
        self.assertNotIn('secret\nmore', text)
        self.assertIn('redacted', text)


if __name__ == '__main__':
    unittest.main()
