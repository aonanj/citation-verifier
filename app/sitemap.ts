import type { MetadataRoute } from 'next';

const siteOrigin = process.env.NEXT_PUBLIC_SITE_URL ?? 'https://www.jurischeck.com';

export default function sitemap(): MetadataRoute.Sitemap {
  const lastModified = new Date();
  const routes = ['/', '/results', '/payments/success', '/payments/cancelled'];

  return routes.map((path) => ({
    url: new URL(path, siteOrigin).toString(),
    lastModified,
    changeFrequency: path === '/' ? 'weekly' : 'monthly',
    priority: path === '/' ? 1 : 0.6,
  }));
}
