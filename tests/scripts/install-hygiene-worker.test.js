const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const REPO_ROOT = path.join(__dirname, '..', '..');
const SCRIPT = path.join(REPO_ROOT, 'scripts', 'install-hygiene-worker.sh');

function createTempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function cleanup(dirPath) {
  fs.rmSync(dirPath, { recursive: true, force: true });
}

function run(homeDir, args = []) {
  try {
    const stdout = execFileSync('bash', [SCRIPT, ...args], {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        HOME: homeDir,
      },
      encoding: 'utf8',
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 10000,
    });
    return { code: 0, stdout, stderr: '' };
  } catch (error) {
    return {
      code: error.status || 1,
      stdout: error.stdout || '',
      stderr: error.stderr || '',
    };
  }
}

function test(name, fn) {
  try {
    fn();
    console.log(`  \u2713 ${name}`);
    return true;
  } catch (error) {
    console.log(`  \u2717 ${name}`);
    console.log(`    Error: ${error.message}`);
    return false;
  }
}

function runTests() {
  console.log('\n=== Testing install-hygiene-worker.sh ===\n');
  let passed = 0;
  let failed = 0;

  if (process.platform === 'win32') {
    console.log('  - skipped on Windows; install-hygiene-worker.sh is a bash/launchd surface');
    console.log(`\nResults: Passed: ${passed}, Failed: ${failed}`);
    process.exit(0);
  }

  if (test('writes plist with HOME, PYTHON_PATH, and REPO_ROOT substituted', () => {
    const homeDir = createTempDir('install-hygiene-home-');
    try {
      const result = run(homeDir);
      assert.strictEqual(result.code, 0, result.stderr);
      const dest = path.join(homeDir, 'Library', 'LaunchAgents', 'com.ecc.hygiene.plist');
      assert.ok(fs.existsSync(dest), 'plist should be written');
      const content = fs.readFileSync(dest, 'utf8');
      assert.ok(!content.includes('<HOME>'));
      assert.ok(!content.includes('<PYTHON_PATH>'));
      assert.ok(!content.includes('<REPO_ROOT>'));
      assert.ok(content.includes(path.join(REPO_ROOT, 'scripts', 'hygiene-worker.py')));
    } finally {
      cleanup(homeDir);
    }
  })) passed++; else failed++;

  if (test('prints manual load instructions when --load is not passed', () => {
    const homeDir = createTempDir('install-hygiene-home-');
    try {
      const result = run(homeDir);
      assert.strictEqual(result.code, 0, result.stderr);
      assert.ok(result.stdout.includes('To load the agent now, run:'));
      assert.ok(result.stdout.includes('launchctl load'));
    } finally {
      cleanup(homeDir);
    }
  })) passed++; else failed++;

  console.log(`\nResults: Passed: ${passed}, Failed: ${failed}`);
  process.exit(failed > 0 ? 1 : 0);
}

runTests();
