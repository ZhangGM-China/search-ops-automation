const response = await fetch('http://127.0.0.1:8124/health', {signal: AbortSignal.timeout(4000)});
const body = await response.json();
if (!response.ok || body.runtime !== 'pi-sdk' || body.kgConfigured !== true) process.exit(1);
