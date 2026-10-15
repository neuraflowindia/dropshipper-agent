// The offline Python producers that portal tests start (fixture builders only):
// PRODUCT_RADAR_PYTHON when set (for example a build copy without its own
// .venv), else the project's .venv at the repository root.
import { fileURLToPath } from 'node:url';

export const ROOT = fileURLToPath(new URL('..', import.meta.url));
export const PYTHON = process.env.PRODUCT_RADAR_PYTHON || ROOT + '.venv/bin/python';
