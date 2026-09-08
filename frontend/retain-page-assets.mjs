import { readdirSync, statSync, unlinkSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
export function retainPageAssets(directory = fileURLToPath(new URL('./dist/assets', import.meta.url))) {
  const current = new Set();
  return {
    name: 'retain-recent-page-assets',
    generateBundle(_options, bundle) {
      for (const name of Object.keys(bundle)) current.add(name.replace(/^assets\//, ''));
    },
    closeBundle() {
      const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
      for (const entry of readdirSync(directory, {withFileTypes:true})) {
        if (!entry.isFile() || current.has(entry.name) || !/[-.][A-Za-z0-9_-]{8,}\.(js|css)$/.test(entry.name)) continue;
        const path = join(directory, entry.name);
        if (statSync(path).mtimeMs < cutoff) unlinkSync(path);
      }
    },
  };
}
