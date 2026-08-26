/* Non-secret deployment metadata. Never place bearer values or private facts here. */
(function (global) {
  'use strict';

  const config = {
    deploymentMode: 'HYBRID_PRIVATE',
    apiBaseUrl: '',
    allowedApiOrigins: [],
    ssePath: '/api/stream',
    frontendBuild: 'development',
    expectedApiMajor: 1,
    expectedEngineId: 'stock-tracker-local',
    allowApiOriginOverride: false,
    allowPrivateBrowserCache: false,
    healthPollMs: 15000
  };
  config.allowedApiOrigins = Object.freeze(config.allowedApiOrigins.slice());
  global.STOCK_TRACKER_RUNTIME = Object.freeze(config);
})(window);
