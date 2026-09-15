// Refresh the routing truth table from the REAL backend rule so the harness can
// never assert a routing decision the production code would not make. If Python
// is unavailable, fall back to the committed routing.generated.js and warn.
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

module.exports = async () => {
  const repoRoot = path.resolve(__dirname, '..', '..');
  const script = path.join(repoRoot, 'tests', 'e2e', 'generate_fixtures.py');
  const venvPy = path.join(repoRoot, '.venv', 'bin', 'python');
  const py = fs.existsSync(venvPy) ? venvPy : 'python3';
  try {
    execFileSync(py, [script], { cwd: repoRoot, stdio: 'inherit' });
  } catch (err) {
    const generated = path.join(__dirname, 'harness', 'routing.generated.js');
    if (!fs.existsSync(generated)) {
      throw new Error(
        `Could not generate routing fixtures and no committed file exists at ${generated}: ${err.message}`,
      );
    }
    console.warn('[e2e] using committed routing.generated.js (fixture regeneration failed):', err.message);
  }
};
