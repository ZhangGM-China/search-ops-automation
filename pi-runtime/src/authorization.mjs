import path from 'node:path';

const BUSINESS_TOOLS = new Set([
  'read_today_context',
  'read_brand_metrics',
  'read_top_query_detail',
  'read_top_query_events',
  'read_abnormal_query_logs',
  'read_inventory_monitoring',
  'search_products',
  'get_product_detail',
  'prepare_action',
]);

const PATH_TOOLS = new Set(['read', 'write', 'edit', 'grep', 'find', 'ls']);

export function normalizePermissionScope(scope = {}) {
  const brands = Array.isArray(scope.brands)
    ? [...new Set(scope.brands.map((value) => String(value ?? '').trim()).filter(Boolean))]
    : [];
  const tools = Array.isArray(scope.tools)
    ? [...new Set(scope.tools.map((value) => String(value ?? '').trim()).filter(Boolean))]
    : [];
  const workdir = scope.workdir ? path.resolve(String(scope.workdir)) : undefined;
  return { brands, tools, ...(workdir ? { workdir } : {}) };
}

function assertWorkspacePath(value, workdir) {
  if (!workdir || !value) return;
  const resolved = path.resolve(workdir, String(value));
  const relative = path.relative(workdir, resolved);
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error('Path is outside the account workspace');
  }
}

function assertWorkspaceCommand(command, workdir) {
  if (!workdir) return;
  const raw = String(command ?? '');
  if (/(^|\s)(?:sudo|ssh|scp)\b/i.test(raw)) {
    throw new Error('Privilege escalation or remote shell is not allowed from the account workspace');
  }
  if (/(^|\s)(?:curl|wget)\b/i.test(raw) && !/https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?\/api\//i.test(raw)) {
    throw new Error('Network access is limited to the local AIOPS API');
  }
  const absolutePaths = raw.match(/(?:^|\s)(\/[^\s'";|&]+)/g) ?? [];
  for (const token of absolutePaths) {
    assertWorkspacePath(token.trim(), workdir);
  }
  if (/(^|\s|\/)(?:\.\.(?:\/|\\|$))/.test(raw)) {
    throw new Error('Shell command escapes the account workspace');
  }
}

export function assertToolCallAllowed(call, rawScope = {}) {
  const scope = normalizePermissionScope(rawScope);
  const toolName = String(call?.toolName ?? '').trim();
  const input = call?.input && typeof call.input === 'object' ? call.input : {};
  if (scope.tools.length && !scope.tools.includes(toolName) && (BUSINESS_TOOLS.has(toolName) || PATH_TOOLS.has(toolName) || toolName === 'bash')) {
    throw new Error(`Tool ${toolName} is not enabled for this account`);
  }

  if (BUSINESS_TOOLS.has(toolName)) {
    const requestedBrand = String(input.brand ?? '').trim();
    if (requestedBrand && !scope.brands.includes(requestedBrand)) {
      throw new Error(`Brand ${requestedBrand} is not accessible for this account`);
    }
  }
  if (PATH_TOOLS.has(toolName)) {
    assertWorkspacePath(input.path, scope.workdir);
  }
  if (toolName === 'bash') {
    assertWorkspaceCommand(input.command, scope.workdir);
  }
  return true;
}

export { BUSINESS_TOOLS, PATH_TOOLS };
