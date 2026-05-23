"use client";

import { Canvas } from "@react-three/fiber";
import { OrbitControls, ContactShadows } from "@react-three/drei";
import IngestionNode from "./IngestionNode";
import AgentSwarm from "./AgentSwarm";
import DagWorkflow from "./DagWorkflow";
import DataFlowDots from "./DataFlowDots";
import OverseerEye from "./OverseerEye";

export default function AegisHero() {
  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 0 }}>
      <Canvas
        camera={{ position: [0, 2.5, 13], fov: 50 }}
        style={{ background: "#F8FAFC" }}
        gl={{
          antialias: true,
          alpha: false,
          powerPreference: "high-performance",
          // Remove toneMapping number — use string-safe default
        }}
        shadows
      >
        {/* ── Studio Lighting ─────────────────────────────── */}
        <ambientLight intensity={1.2} color="#F8FAFC" />

        {/* Key light */}
        <directionalLight
          position={[6, 14, 8]}
          intensity={1.8}
          castShadow
          shadow-mapSize-width={1024}
          shadow-mapSize-height={1024}
          shadow-camera-left={-18}
          shadow-camera-right={18}
          shadow-camera-top={12}
          shadow-camera-bottom={-8}
          shadow-camera-far={50}
          color="#FFF5E6"
        />

        {/* Fill light */}
        <directionalLight position={[-8, 5, -4]} intensity={0.5} color="#E8F0FE" />
        <directionalLight position={[0, -2, -8]} intensity={0.2} color="#FFFFFF" />

        {/* Subtle floor grid — inline mesh avoids drei version issues */}
        <gridHelper
          args={[40, 50, "#CBD5E1", "#E2E8F0"]}
          position={[0, -3.1, 0]}
        />

        {/* Contact shadows */}
        <ContactShadows
          position={[0, -3.0, 0]}
          opacity={0.15}
          scale={28}
          blur={2}
          far={5}
          color="#0F172A"
        />

        {/* Scene */}
        <IngestionNode />
        <AgentSwarm />
        <DagWorkflow />
        <DataFlowDots />
        <OverseerEye />

        <OrbitControls
          enablePan={false}
          minDistance={7}
          maxDistance={18}
          minPolarAngle={Math.PI / 5}
          maxPolarAngle={Math.PI * 0.62}
          autoRotate
          autoRotateSpeed={0.18}
          enableDamping
          dampingFactor={0.06}
        />
      </Canvas>
    </div>
  );
}
