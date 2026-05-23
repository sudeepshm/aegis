"use client";

import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";

export default function IngestionNode() {
  const outerRef = useRef<THREE.Mesh>(null);
  const innerRef = useRef<THREE.Mesh>(null);
  const ring1Ref = useRef<THREE.Mesh>(null);
  const ring2Ref = useRef<THREE.Mesh>(null);

  useFrame((state, delta) => {
    const t = state.clock.elapsedTime;
    if (outerRef.current) {
      outerRef.current.rotation.y += delta * 0.25;
      outerRef.current.rotation.x = Math.sin(t * 0.4) * 0.07;
      outerRef.current.position.y = Math.sin(t * 0.55) * 0.08;
    }
    if (innerRef.current) {
      innerRef.current.rotation.y -= delta * 0.18;
      innerRef.current.rotation.z += delta * 0.12;
    }
    if (ring1Ref.current) ring1Ref.current.rotation.y += delta * 0.6;
    if (ring2Ref.current) ring2Ref.current.rotation.z += delta * 0.4;
  });

  return (
    <group position={[-5.5, 0, 0]}>
      <mesh ref={outerRef} castShadow receiveShadow>
        <boxGeometry args={[1.7, 1.7, 1.7]} />
        <meshStandardMaterial color="#0F172A" metalness={0.88} roughness={0.1} />
      </mesh>

      <mesh ref={innerRef}>
        <octahedronGeometry args={[0.55, 0]} />
        <meshStandardMaterial
          color="#1E40AF"
          metalness={0.7}
          roughness={0.12}
          emissive="#1D4ED8"
          emissiveIntensity={0.15}
        />
      </mesh>

      {/* Orange scanning ring */}
      <mesh ref={ring1Ref}>
        <torusGeometry args={[1.25, 0.022, 10, 56]} />
        <meshStandardMaterial
          color="#F97316"
          emissive="#EA580C"
          emissiveIntensity={0.2}
          transparent
          opacity={0.65}
        />
      </mesh>

      {/* Gold orbit ring */}
      <mesh ref={ring2Ref} rotation={[Math.PI / 2, 0, 0]}>
        <torusGeometry args={[1.05, 0.016, 10, 56]} />
        <meshStandardMaterial color="#D4AF37" metalness={0.9} roughness={0.12} transparent opacity={0.5} />
      </mesh>
    </group>
  );
}
