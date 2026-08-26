/*
 * Copy deployment metadata into runtime-config.js during static-site release.
 * This file is public. Never add STOCK_TRACKER_PRIVATE_ACCESS, portfolio facts,
 * provider credentials or any bearer value here.
 */
(function (global) {
  'use strict';

  const config = {
    deploymentMode: 'HYBRID_PRIVATE',
    apiBaseUrl: 'https://your-engine.your-tailnet.ts.net',
    allowedApiOrigins: ['https://your-engine.your-tailnet.ts.net'],
    ssePath: '/api/stream',
    frontendBuild: 'replace-with-frontend-git-commit',
    expectedApiMajor: 1,
    expectedEngineId: 'stock-tracker-local',
    allowApiOriginOverride: false,
    allowPrivateBrowserCache: false,
    healthPollMs: 15000
  };
  config.allowedApiOrigins = Object.freeze(config.allowedApiOrigins.slice());
  global.STOCK_TRACKER_RUNTIME = Object.freeze(config);
})(window);
