'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const readline = require('node:readline');

const HOME = os.homedir();
const UI_URI = 'ui://my-usage/dashboard';
const SCANNER = path.join(__dirname, 'scan_sessions.py');
const PYTHON = process.env.MY_USAGE_PYTHON || (fs.existsSync('/usr/bin/python3') ? '/usr/bin/python3' : 'python3');
const CLIENT_BUNDLE = fs.readFileSync(path.join(__dirname, 'client.bundle.js'), 'utf8').replace(/<\/script/gi, '<\\/script');
const DASHBOARD = fs.readFileSync(path.join(__dirname, 'dashboard.html'), 'utf8')
  .replace(/<!-- MY_USAGE_SNAPSHOT_START -->[\s\S]*?<!-- MY_USAGE_SNAPSHOT_END -->/, '<!-- MY_USAGE_SNAPSHOT_START --><script type="application/json" id="usage-snapshot">{}</script><!-- MY_USAGE_SNAPSHOT_END -->')
  .replace('</body>', () => `<script>${CLIENT_BUNDLE}</script></body>`);

function scanIndex(args = []) {
  const stdout = execFileSync(PYTHON, [SCANNER, ...args], {
    encoding: 'utf8',
    maxBuffer: 128 * 1024 * 1024,
    env: { ...process.env, HOME },
  });
  return JSON.parse(stdout);
}
function toolResult(data) {
  return { content: [{ type: 'text', text: JSON.stringify(data) }], structuredContent: data };
}
function send(message) { process.stdout.write(JSON.stringify(message) + '\n'); }

const tools = [
  {
    name: 'usage.dashboard',
    title: 'Codex Usage Dashboard',
    description: 'Open an interactive dashboard for locally indexed Codex model, effort, and token usage. It checks session file metadata and incrementally updates the index when new or changed files are found.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    outputSchema: { type: 'object', properties: { totals: { type: 'object' }, sessions: { type: 'array' } }, required: ['totals', 'sessions'] },
    _meta: { ui: { resourceUri: UI_URI, visibility: ['model', 'app'] }, 'openai/ui': { entrypoints: [{ type: 'global' }] } },
    annotations: { readOnlyHint: false, openWorldHint: false, title: 'Codex Usage Dashboard' },
  },
  {
    name: 'usage.scan',
    title: 'Scan Codex Sessions',
    description: 'Incrementally scan new or changed local Codex session files and update the private My Usage SQLite index. Conversation text is never stored.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    outputSchema: { type: 'object', properties: { scan: { type: 'object' }, indexedSessions: { type: 'number' }, sessionsWithTokenData: { type: 'number' }, databasePath: { type: 'string' } }, required: ['scan', 'indexedSessions', 'sessionsWithTokenData', 'databasePath'] },
    _meta: { ui: { visibility: ['model', 'app'] } },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false, title: 'Scan Codex Sessions' },
  },
];

async function handle(msg) {
  const id = msg.id;
  const method = msg.method;
  const params = msg.params || {};
  if (method === 'initialize') return send({ jsonrpc: '2.0', id, result: { protocolVersion: params.protocolVersion || '2025-11-25', capabilities: { tools: {}, resources: {} }, serverInfo: { name: 'my-usage', version: '0.2.5' } } });
  if (method === 'notifications/initialized' || method === 'notifications/cancelled') return;
  if (method === 'ping') return send({ jsonrpc: '2.0', id, result: {} });
  if (method === 'tools/list') return send({ jsonrpc: '2.0', id, result: { tools } });
  if (method === 'resources/list') return send({ jsonrpc: '2.0', id, result: { resources: [{ uri: UI_URI, name: 'Codex Usage Dashboard', mimeType: 'text/html;profile=mcp-app', description: 'Interactive local Codex model usage dashboard' }] } });
  if (method === 'resources/read') {
    if (params.uri !== UI_URI) return send({ jsonrpc: '2.0', id, error: { code: -32602, message: 'Unknown resource URI' } });
    return send({ jsonrpc: '2.0', id, result: { contents: [{ uri: UI_URI, mimeType: 'text/html;profile=mcp-app', text: DASHBOARD, _meta: { 'openai/ui': { availableDisplayModes: ['fullscreen', 'inline'], preferredDisplayMode: 'fullscreen' } } }] } });
  }
  if (method === 'tools/call') {
    try {
      if (params.name === 'usage.dashboard') {
        scanIndex(['sync']);
        return send({ jsonrpc: '2.0', id, result: toolResult(scanIndex(['read'])) });
      }
      if (params.name === 'usage.scan') {
        const scan = scanIndex(['sync']);
        const indexed = scanIndex(['read']);
        return send({ jsonrpc: '2.0', id, result: toolResult({
          scan,
          indexedSessions: indexed.totals.sessions,
          sessionsWithTokenData: indexed.totals.sessionsWithTokenData,
          databasePath: scan.databasePath,
        }) });
      }
      return send({ jsonrpc: '2.0', id, error: { code: -32602, message: 'Unknown tool' } });
    } catch (error) {
      const detail = error.stderr ? String(error.stderr).trim() : error.message;
      return send({ jsonrpc: '2.0', id, result: { isError: true, content: [{ type: 'text', text: `Could not update the local usage index: ${detail}` }] } });
    }
  }
  if (id !== undefined) send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
}
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', line => { try { const msg = JSON.parse(line); Promise.resolve(handle(msg)).catch(err => { if (msg.id !== undefined) send({ jsonrpc: '2.0', id: msg.id, error: { code: -32603, message: err.message } }); }); } catch { /* stdout is reserved for MCP messages */ } });
