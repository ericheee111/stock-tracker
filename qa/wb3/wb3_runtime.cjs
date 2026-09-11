'use strict';
const { spawn } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function loadPlaywright() {
  const configured = process.env.PLAYWRIGHT_MODULE || process.env.PLAYWRIGHT_PATH;
  try { return require(configured || 'playwright'); }
  catch (error) { throw new Error('PLAYWRIGHT_UNAVAILABLE: ' + error.message); }
}

function reportDirectory(kind) {
  const root = process.env.WB3_REPORT_DIR || fs.mkdtempSync(path.join(os.tmpdir(), 'wb3-' + kind + '-'));
  fs.mkdirSync(root, { recursive: true });
  return root;
}

async function bounded(action, milliseconds = 5000) {
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(action), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('CLEANUP_TIMEOUT')), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

async function stopServer(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await bounded(() => new Promise((resolve, reject) => {
    child.once('close', resolve);
    child.once('error', reject);
    child.kill();
  }));
}

async function startServer(scenario) {
  const runId = randomUUID();
  const port = process.env.WB3_PORT || '0';
  const timeout = Number(process.env.WB3_READY_TIMEOUT_MS || 15000);
  if (!/^\d+$/.test(port) || Number(port) > 65535 || !Number.isFinite(timeout) || timeout <= 0) {
    throw new Error('INVALID_SERVER_CONFIGURATION');
  }
  const child = spawn(process.env.WB3_NODE || process.execPath,
    [path.join(__dirname, 'wb3_server.cjs')], {
      env: { ...process.env, WB3_SCENARIO: scenario, WB3_PORT: port, WB3_RUN_ID: runId },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  let timer, output = '', pending = '';
  try {
    const url = await new Promise((resolve, reject) => {
      let settled = false;
      function finish(error, value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) reject(error); else resolve(value);
      }
      timer = setTimeout(() => finish(new Error('SERVER_READY_TIMEOUT: ' + output)), timeout);
      child.on('error', (error) => finish(new Error('SERVER_SPAWN_ERROR: ' + error.message)));
      child.on('close', (code) => finish(new Error('SERVER_START_FAILED exit=' + code + ': ' + output)));
      child.stderr.on('data', (data) => { output += String(data); });
      child.stdout.on('data', (data) => {
        output += String(data); pending += String(data);
        const lines = pending.split('\n'); pending = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('WB3_READY ')) continue;
          try {
            const ready = JSON.parse(line.slice(10));
            if (ready.runId !== runId || ready.pid !== child.pid || ready.scenario !== scenario ||
                !Number.isInteger(ready.port) || ready.port < 1 || ready.port > 65535 ||
                (Number(port) !== 0 && ready.port !== Number(port))) continue;
            finish(null, 'http://127.0.0.1:' + ready.port);
          } catch { /* Only a complete matching child readiness record is usable. */ }
        }
      });
    });
    return { child, url, runId, stop: () => stopServer(child) };
  } catch (error) {
    try { await stopServer(child); } catch (cleanup) { error.message += '; cleanup=' + cleanup.message; }
    throw error;
  } finally { clearTimeout(timer); }
}

module.exports = { loadPlaywright, reportDirectory, bounded, startServer, stopServer };
