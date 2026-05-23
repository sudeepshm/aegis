"use client";

import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";

const SATELLITES = [
  { metal: "#C8C8C8", orbitR: 2.0, speed: 0.38, tilt: 0.3,  size: 0.18 },
  { metal: "#D4AF37", orbitR: 2.2, speed: -0.29, tilt: 0.7, size: 0.22 },
  { metal: "#B87333", orbitR: 1.8, speed: 0.55, tilt: 1.1,  size: 0.16 },
  { metal: "#E8E8E8", orbitR: 2.4, speed: -0.42, tilt: 0.5, size: 0.19 },
  { metal: "#CD7F32", orbitR: 2.0, speed: 0.33, tilt: 1.4,  size: 0.17 },
];

export default function AgentSwarm() {
  const coreRef = useRef<THREE.Mesh>(null);
  const innerCoreRef = useRef<THREE.Mesh>(null);
  const groupRef = useRef<THREE.Group>(null);
  const satRefs = useRef<THREE.Mesh[]>([]);

  useFrame((state) => {
    const t = state.clock.elapsedTime;

    if (coreRef.current) {
      coreRef.current.rotation.y += 0.004;
      coreRef.current.rotation.x = Math.sin(t * 0.4) * 0.08;
      coreRef.current.scale.setScalar(1 + Math.sin(t * 1.2) * 0.025);
    }
    if (innerCoreRef.current) {
      innerCoreRef.current.rotation.y -= 0.007;
      innerCoreRef.current.rotation.z += 0.004;
    }
    if (groupRef.current) {
      groupRef.current.position.y = Math.sin(t * 0.35) * 0.08;
    }

    satRefs.current.forEach((sat, i) => {
      if (!sat) return;
      const cfg = SATELLITES[i];
      const angle = t * cfg.speed + (i / SATELLITES.length) * Math.PI * 2;
      sat.position.x = Math.cos(angle) * cfg.orbitR;
      sat.position.y = Math.sin(angle * 0.5 + cfg.tilt) * 0.65;
      sat.position.z = Math.sin(angle) * cfg.orbitR;
    });
  });

  return (
    <group ref={groupRef} position={[0, 0, 0]}>
      {/* Core sphere — deep navy */}
      <mesh ref={coreRef} castShadow>
        <sphereGeometry args={[0.9, 32, 32]} />
        <meshStandardMaterial color="#0F172A" metalness={0.88} roughness={0.1} />
      </mesh>

      {/* Inner icosahedron — cobalt blue */}
      <mesh ref={innerCoreRef}>
        <icosahedronGeometry args={[0.52, 1]} />
        <meshStandardMaterial
          color="#1E40AF"
          metalness={0.65}
          roughness={0.15}
          emissive="#1E3A8A"
          emissiveIntensity={0.18}
        />
      </mesh>

      {/* Data pathway rings — 3 perpendicular */}
      {[0, Math.PI / 3, Math.PI * 2 / 3].map((angle, i) => (
        <mesh key={i} rotation={[angle, angle * 0.5, 0]}>
          <torusGeometry args={[0.68, 0.012, 8, 64]} />
          <meshStandardMaterial
            color="#3B82F6"
            metalness={0.3}
            roughness={0.5}
            emissive="#2563EB"
            emissiveIntensity={0.2}
            transparent
            opacity={0.65}
          />
        </mesh>
      ))}

      {/* Orbit guide ring */}
      <mesh rotation={[Math.PI / 2, 0, 0]}>
        <torusGeometry args={[1.4, 0.016, 8, 80]} />
        <meshStandardMaterial color="#CBD5E1" metalness={0.3} roughness={0.6} transparent opacity={0.4} />
      </mesh>

      {/* Precious metal satellites */}
      {SATELLITES.map((cfg, i) => (
        <mesh key={i} castShadow ref={el => { if (el) satRefs.current[i] = el; }}>
          <sphereGeometry args={[cfg.size, 16, 16]} />
          <meshStandardMaterial color={cfg.metal} metalness={0.92} roughness={0.08} />
        </mesh>
      ))}
    </group>
  );
}
