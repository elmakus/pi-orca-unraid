import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

const extension = '/opt/pi-orca-extensions/stream-stall-recovery.mjs';
const waitForServer = server => new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const send = (res, event) => res.write(`data: ${JSON.stringify(event)}\n\n`);

async function testCase(withExtension) {
  let attempts = 0;
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let payload = '';
    for await (const chunk of req) payload += chunk;
    const parsed = JSON.parse(payload);
    requests.push(parsed.input ?? []);
    attempts++;
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    send(res, { type: 'response.created', response: { id: `resp-${attempts}`, status: 'in_progress', output: [] } });
    send(res, { type: 'response.output_item.added', output_index: 0, item: { type: 'message', id: `msg-${attempts}`, role: 'assistant', status: 'in_progress', content: [] } });
    send(res, { type: 'response.output_text.delta', output_index: 0, content_index: 0, delta: attempts === 1 ? 'BEFORE_' : 'AFTER' });
    if (attempts === 1) {
      send(res, { type: 'response.failed', response: { id: 'resp-1', status: 'failed', error: { code: 'stream_incomplete', message: 'Upstream websocket closed before response.completed' } } });
    } else {
      send(res, { type: 'response.completed', response: { id: `resp-${attempts}`, status: 'completed', output: [], usage: { input_tokens: 100, output_tokens: 2, total_tokens: 102 } } });
    }
    res.end();
  });
  await waitForServer(server);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-recovery-e2e-'));
  try {
    const configDir = path.join(dir, '.pi/agent');
    fs.mkdirSync(configDir, { recursive: true });
    fs.writeFileSync(path.join(configDir, 'models.json'), JSON.stringify({ providers: { mock: {
      api: 'openai-responses', baseUrl: `http://127.0.0.1:${server.address().port}/v1`, apiKey: 'local-test',
      models: [{ id: 'mock', reasoning: false, input: ['text'], contextWindow: 16000, maxTokens: 1024 }],
    } } }));
    fs.writeFileSync(path.join(configDir, 'settings.json'), JSON.stringify({
      defaultProvider: 'mock', defaultModel: 'mock', defaultThinkingLevel: 'off',
      retry: { enabled: true, maxRetries: 0, baseDelayMs: 500, maxAgentDelayMs: 60000 },
      extensions: withExtension ? [extension] : [],
    }));
    const output = await new Promise((resolve, reject) => {
      const child = spawn('pi', ['--no-session', '--no-tools', '--no-mcp', '--offline', '--print', 'Respond briefly'], {
        cwd: dir, env: { ...process.env, HOME: dir, PI_OFFLINE: '1' }, stdio: ['ignore', 'pipe', 'pipe'],
      });
      let stdout = ''; let stderr = '';
      child.stdout.on('data', d => { stdout += d; });
      child.stderr.on('data', d => { stderr += d; });
      child.on('error', reject);
      child.on('close', code => resolve({ code, stdout, stderr }));
    });
    if (withExtension) {
      assert.equal(attempts, 2, 'Must continue exactly once after committed text');
      assert.match(output.stdout, /AFTER/);
      const second = JSON.stringify(requests[1].filter(item => item.role !== 'system'));
      assert.match(second, /BEFORE_/, 'Failed partial must appear in follow-up prompt');
      assert.match(second, /Do not|WITHOUT/, 'Follow-up must prohibit duplicate output');
    } else {
      assert.equal(attempts, 1, 'Unpatched Pi must not auto-resume after retry budget is zero');
      assert.match(output.stdout + output.stderr, /stream_incomplete/);
    }
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('baseline Pi stops on interrupted response', () => testCase(false));
test('stream recovery resumes with complete partial text context', () => testCase(true));