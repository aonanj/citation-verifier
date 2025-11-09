import type { MetadataRoute } from 'next';

const siteOrigin = (process.env.NEXT_PUBLIC_SITE_URL ?? 'https://www.jurischeck.com').replace(/\/+$/, '');

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: '/',
      },
    ],
    sitemap: `${siteOrigin}/sitemap.xml`,
    host: siteOrigin,
  };
}
