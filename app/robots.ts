import type { MetadataRoute } from 'next';

const siteOrigin = 'https://www.jurischeck.com';

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: '/',
      },
    ],
    sitemap: 'https://www.jurischeck.com/sitemap.xml',
  };
}
