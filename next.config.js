// next.config.js
module.exports = {
  env: {
    BACKEND_URL: process.env.BACKEND_URL,
  },
  experimental: {
    serverActions: { allowedOrigins: ['*'] },
  },
};