import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
execFileSync('python3', [fileURLToPath(new URL('./check_site.py', import.meta.url))], { stdio: 'inherit' });
