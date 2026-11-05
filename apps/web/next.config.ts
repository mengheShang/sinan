import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  // Standalone output, so the Docker image does not need the full node_modules
  output: "standalone",
};

export default nextConfig;
