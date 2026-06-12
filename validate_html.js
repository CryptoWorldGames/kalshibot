// Pre-commit guard: validate every <script> block in index.html parses cleanly.
// A single syntax error here can break the Update button — your remote lifeline —
// forcing physical access to fix. This blocks such commits BEFORE they're pushed.
const fs = require('fs');
const files = ['index.html', 'mobile.html'];
let totalErrors = 0;
for (const file of files) {
  if (!fs.existsSync(file)) continue;
  const html = fs.readFileSync(file, 'utf8');
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
  scripts.forEach((m, i) => {
    const code = m[1];
    if (!code.trim()) return;
    try { new Function(code); }
    catch(e) {
      totalErrors++;
      console.error(`✗ ${file} script block ${i}: ${e.message}`);
    }
  });
}
if (totalErrors > 0) {
  console.error(`\n❌ COMMIT BLOCKED: ${totalErrors} JavaScript syntax error(s). Fix before committing.`);
  process.exit(1);
}
console.log('✓ All script blocks valid');
