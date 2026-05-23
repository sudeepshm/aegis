import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Allow react-three-fiber and postprocessing to work without SSR
  transpilePackages: ["three", "@react-three/fiber", "@react-three/drei", "@react-three/postprocessing", "reactflow"],
};

export default nextConfig;
