/* Where the backend lives. Hosted: the Railway service. A local checkout never
 * loads this file: backend/webui/app.py answers /static/config.js itself with
 * an empty base, so the pages call their own server. */
globalThis.SCICOM_API_BASE = 'https://scicom-api.agents.turingplanet.ai';
