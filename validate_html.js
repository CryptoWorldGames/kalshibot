#!/usr/bin/env node
const fs = require('fs');

// Simple validation that the HTML files exist and have content
const files = ['index.html', 'mobile.html'].filter(f => {
  try {
    return fs.existsSync(f) && fs.statSync(f).size > 0;
  } catch {
    return false;
  }
});

if (files.length === 0) {
  console.error('No HTML files found to validate');
  process.exit(1);
}

// Basic checks
for (const file of files) {
  try {
    const content = fs.readFileSync(file, 'utf8');

    // Check that files aren't empty
    if (content.trim().length === 0) {
      console.error(`${file}: File is empty`);
      process.exit(1);
    }

    // Check for basic HTML structure
    if (!content.includes('<html') && !content.includes('<!DOCTYPE')) {
      console.warn(`${file}: Missing HTML doctype/html tag`);
    }
  } catch (e) {
    console.error(`${file}: Error reading file: ${e.message}`);
    process.exit(1);
  }
}

// All checks passed
process.exit(0);
