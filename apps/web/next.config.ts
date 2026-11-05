import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  // 生成独立运行包，Docker 镜像不需要完整的 node_modules
  output: "standalone",
};

export default nextConfig;
