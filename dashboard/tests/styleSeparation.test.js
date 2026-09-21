import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dashboardDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceDirectory = path.join(dashboardDirectory, 'src');

const STYLE_BLOCK_PATTERN = /<style\b((?:"[^"]*"|'[^']*'|[^'">])*)>([\s\S]*?)<\/style\s*>/gi;
const STYLE_SOURCE_ATTRIBUTE = /(?:^|\s)src\s*=\s*(["'])[^"']*\.(?:css|scss)(?:[?#][^"']*)?\1(?=\s|$)/i;
const STYLE_MODULE_DIRECTIVES_ONLY =
  /^\s*(?:@(?:import|use|forward)\b(?:[^;"'{}]+|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')*;\s*)+$/;
const BORDER_RADIUS_DECLARATION = /\bborder-radius\s*:\s*([^;]+);/gi;
const BORDER_RADIUS_VARIABLE = /^var\(--(?:radius-|glass-surface-radius)/;
const JAVASCRIPT_BORDER_RADIUS = /\bborderRadius\s*:/g;

async function findSourceFiles(directory, predicate) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    const entryPath = path.join(directory, entry.name);

    if (entry.isDirectory()) {
      files.push(...await findSourceFiles(entryPath, predicate));
    } else if (entry.isFile() && predicate(entry.name)) {
      files.push(entryPath);
    }
  }

  return files;
}

function isSeparatedStyle(attributes, content) {
  const hasExternalSource = STYLE_SOURCE_ATTRIBUTE.test(attributes);
  const hasOnlyModuleDirectives = STYLE_MODULE_DIRECTIVES_ONLY.test(content);

  return (hasExternalSource && content.trim() === '') || hasOnlyModuleDirectives;
}

test('style blocks are externalized', async () => {
  const vueFiles = await findSourceFiles(sourceDirectory, (name) => name.endsWith('.vue'));
  const violations = [];
  let styleBlockCount = 0;

  for (const vueFile of vueFiles) {
    const source = await readFile(vueFile, 'utf8');
    const relativeVuePath = path.relative(sourceDirectory, vueFile).split(path.sep).join('/');
    const componentName = path.basename(vueFile, '.vue');

    for (const match of source.matchAll(STYLE_BLOCK_PATTERN)) {
      const [, attributes, content] = match;

      styleBlockCount += 1;

      if (!isSeparatedStyle(attributes, content)) {
        const styleStartLine = source.slice(0, match.index).split('\n').length;
        violations.push(
          `${relativeVuePath}:${styleStartLine} contains inline styles. Move the styles to src/assets/css/${componentName}.scss.`,
        );
      }
    }
  }

  assert.ok(
    styleBlockCount > 0,
    'Expected to scan at least one <style> block.',
  );
  assert.equal(
    violations.length,
    0,
    `Found styles that must be externalized:\n${violations.join('\n')}`,
  );
});

test('border radius values are centralized in theme.scss', async () => {
  const styleExtensions = new Set(['.css', '.scss', '.vue', '.js']);
  const sourceFiles = await findSourceFiles(sourceDirectory, (name) => styleExtensions.has(path.extname(name)));
  const themeFile = path.join(sourceDirectory, 'assets', 'css', 'theme.scss');
  const violations = [];

  for (const sourceFile of sourceFiles) {
    if (sourceFile === themeFile) {
      continue;
    }

    const source = await readFile(sourceFile, 'utf8');
    const relativePath = path.relative(sourceDirectory, sourceFile).split(path.sep).join('/');

    for (const match of source.matchAll(BORDER_RADIUS_DECLARATION)) {
      const value = match[1].trim();

      if (!BORDER_RADIUS_VARIABLE.test(value)) {
        const line = source.slice(0, match.index).split('\n').length;
        violations.push(`${relativePath}:${line} uses border-radius ${value}. Use a radius variable from theme.scss.`);
      }
    }

    for (const match of source.matchAll(JAVASCRIPT_BORDER_RADIUS)) {
      const line = source.slice(0, match.index).split('\n').length;
      violations.push(`${relativePath}:${line} uses borderRadius in script/template code. Move the radius to theme-backed styles.`);
    }
  }

  assert.equal(
    violations.length,
    0,
    `Found radius values outside theme.scss:\n${violations.join('\n')}`,
  );
});

test('style externalization classification accepts external sources and module directives', () => {
  const validCases = [
    ['src="./styles.css"', ''],
    ['src="./styles.scss"', ''],
    ['', '@import "./base.css";'],
    ['', '@import "./base.css";\n@import "./theme.css";'],
    ['', '@use "./tokens";'],
    ['', '@use "./tokens"; @forward "./components";'],
    ['', '@forward "./components";'],
    ['', '\n  @import "./base.css";\n@use "./tokens";\n@forward "./components";\n'],
  ];

  for (const [attributes, content] of validCases) {
    assert.equal(isSeparatedStyle(attributes, content), true, `${attributes} ${content}`);
  }
});

test('style externalization classification rejects inline declarations', () => {
  const invalidCases = [
    ['', '.button { color: red; }'],
    ['', '$color: red;'],
    ['', '@media (min-width: 768px) { .button { color: red; } }'],
    ['', '@import "./base.css"; .button { color: red; }'],
    ['src="./styles.scss"', '.button { color: red; }'],
  ];

  for (const [attributes, content] of invalidCases) {
    assert.equal(isSeparatedStyle(attributes, content), false, `${attributes} ${content}`);
  }
});
