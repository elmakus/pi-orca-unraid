import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('guard_committed_output', ROOT / 'scripts/guard-committed-output.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

class GuardTests(unittest.TestCase):
    def test_rewrites_exactly_three_guard_locations(self):
        src = '\n'.join([
            'return this._isRetryableError(message);',
            'if (this._isRetryableError(message) && (await this._prepareRetry(message))) {',
            '    _isRetryableError(message) {',
        ]) + '\n'
        patched = guard.guard_source(src)
        self.assertIn('_hasCommittedReplayOutput(message)', patched)
        self.assertIn('b?.type === "toolCall"', patched)
        self.assertIn('later.role === "toolResult"', patched)
        self.assertIn('block?.type === "text"', patched)
        with self.assertRaises(ValueError):
            guard.guard_source(patched)

    def test_effects_and_partial_tool_safety_in_node(self):
        import subprocess
        source = "\n".join([
            'return this._isRetryableError(message);',
            'if (this._isRetryableError(message) && (await this._prepareRetry(message))) {',
            '    _isRetryableError(message) {',
        ]) + "\n"
        patched = guard.guard_source(source)
        start = patched.index('    _hasCommittedReplayOutput(message) {')
        end = patched.index('    _isRetryableError(message) {', start)
        method = patched[start:end]
        js = f"""const assert = require('node:assert/strict');
class Mock {{ {method} }}
const check = (content, tail, expected) => {{
  const assistant = {{role:'assistant', stopReason:'error', content}};
  const instance = new Mock();
  instance.agent = {{state:{{messages:[assistant,...tail]}}}};
  assert.equal(instance._hasCommittedReplayOutput(assistant), expected);
}};
check([],[],false);
check([{{type:'thinking',thinking:'part'}}],[],false);
check([{{type:'toolCall',id:'t1'}}],[],false);
check([{{type:'toolCall',id:'t1'}}],[{{role:'toolResult',toolCallId:'t1'}}],true);
check([{{type:'text',text:'partial output'}}],[],true);
check([{{type:'text',text:'   '}}],[],false);
check([{{type:'image',data:'abc'}}],[],true);
check([{{type:'toolCall',id:'t1'}},{{type:'text',text:'partial'}}],[],true);
"""
        completed = subprocess.run(['node', '-e', js], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_refuses_unknown_source(self):
        with self.assertRaises(ValueError):
            guard.guard_source('bad source')

if __name__ == '__main__':
    unittest.main()
