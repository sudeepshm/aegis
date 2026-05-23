"use client";

import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";

const RING_CONFIGS = [
  { radius: 1.4, tube: 0.055, color: "#B87333", rotSpeed: 0.18, tiltX: 0.4 },
  { radius: 1.1, tube: 0.045, color: "#C8C8C8", rotSpeed: -0.14, tiltX: -0.4 },
  { radius: 0.82, tube: 0.04, color: "#D4AF37", rotSpeed: 0.22, tiltX: 0.3 },
  { radius: 0.6,  tube: 0.03, color: "#B87333", rotSpeed: -0.3, tiltX: -0.3 },
];

const IRIS_LAYERS = [
  { r: 0.42, t: 0.02, color: "#1E3A8A", speed: 0.5 },
  { r: 0.32, t: 0.018, color: "#2563EB", speed: -0.7 },
  { r: 0.22, t: 0.016, color: "#0EA5E9", speed: 1.1 },
  { r: 0.13, t: 0.015, color: "#38BDF8", speed: -1.5 },
];

export default function RegulatoryShield() {
  const shieldGroupRef = useRef<THREE.Group>(null);
  const irisGroupRef = useRef<THREE.Group>(null);
  const ringRefs = useRef<(THREE.Mesh | null)[]>([]);
  const irisRefs = useRef<(THREE.Mesh | null)[]>([]);

  useFrame((state) => {
    const t = state.clock.elapsedTime;
    const { x, y } = state.pointer;

    if (shieldGroupRef.current) {
      shieldGroupRef.current.rotation.y = Math.sin(t * 0.12) * 0.3;
      shieldGroupRef.current.rotation.x = -0.15 + Math.sin(t * 0.08) * 0.06;
      shieldGroupRef.current.position.y = 3.6 + Math.sin(t * 0.5) * 0.12;
    }

    RING_CONFIGS.forEach((cfg, i) => {
      const ring = ringRefs.current[i];
      if (!ring) return;
      ring.rotation.z += cfg.rotSpeed * 0.01;
      ring.rotation.x = cfg.tiltX + Math.sin(t * 0.3 + i) * 0.05;
    });

    if (irisGroupRef.current) {
      irisGroupRef.current.rotation.y += (x * 0.3 - irisGroupRef.current.rotation.y) * 0.06;
      irisGroupRef.current.rotation.x += (-y * 0.2 - irisGroupRef.current.rotation.x) * 0.06;
    }

    IRIS_LAYERS.forEach((layer, i) => {
      const iris = irisRefs.current[i];
      if (iris) iris.rotation.z += layer.speed * 0.008;
    });
  });

  return (
    <group ref={shieldGroupRef} position={[0, 3.6, 0]}>
      {/* Shield face */}
      <mesh>
        <circleGeometry args={[1.55, 48]} />
        <meshStandardMaterial color="#E8EEF5" metalness={0.6} roughness={0.2} side={THREE.DoubleSide} />
      </mesh>

      {/* Metallic rings — StandardMaterial: GPU-safe, still premium under studio lights */}
      {RING_CONFIGS.map((cfg, i) => (
        <mesh key={i} ref={el => { ringRefs.current[i] = el; }}>
          <torusGeometry args={[cfg.radius, cfg.tube, 16, 72]} />
          <meshStandardMaterial color={cfg.color} metalness={0.9} roughness={0.1} />
        </mesh>
      ))}

      {/* Iris group — mouse tracking preserved */}
      <group ref={irisGroupRef}>
        {IRIS_LAYERS.map((layer, i) => (
          <mesh key={i} ref={el => { irisRefs.current[i] = el; }} position={[0, 0, 0.02 * (i + 1)]}>
            <torusGeometry args={[layer.r, layer.t, 12, 48]} />
            <meshStandardMaterial
              color={layer.color}
              metalness={0.5}
              roughness={0.3}
              emissive={layer.color}
              emissiveIntensity={0.25}
            />
          </mesh>
        ))}
        <mesh position={[0, 0, 0.1]}>
          <circleGeometry args={[0.07, 24]} />
          <meshStandardMaterial color="#0F172A" metalness={0.9} roughness={0.05} />
        </mesh>
      </group>

      {/* Gold rim */}
      <mesh>
        <torusGeometry args={[1.55, 0.07, 16, 72]} />
        <meshStandardMaterial color="#D4AF37" metalness={0.95} roughness={0.08} />
      </mesh>
    </group>
  );
}
