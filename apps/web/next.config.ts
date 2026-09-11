import type { NextConfig } from "next";

const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL?.trim();
const isBadRenderLocalUrl =
  process.env.RENDER === "true" &&
  !!configuredApiUrl &&
  /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/i.test(configuredApiUrl);

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  eslint: {
    ignoreDuringBuilds: true,
  },
  env: {
    // A localhost API URL is valid for local development, but it must never
    // leak into a deployed Render browser bundle.
    NEXT_PUBLIC_API_URL:
      isBadRenderLocalUrl || !configuredApiUrl
        ? "https://ai-powered-analytics-platform.onrender.com"
        : configuredApiUrl,
  },
};

export default nextConfig;
