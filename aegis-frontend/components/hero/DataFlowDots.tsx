"use client";

import { useRef, useMemo } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";

const PHASE1_COUNT = 5;
const PHASE2_COUNT = 6;
const DAG_PARTICLE_COUNT = 3;

const INGESTION_TO_CORE = new THREE.QuadraticBezierCurve3(
  new THREE.Vector3(-5.5, -0.2, 0),
  new THREE.Vector3(-2.5, 1.2, 0.5),
  new THREE.Vector3(0, 0, 0)
);

const CORE_TO_DAG = new THREE.QuadraticBezierCurve3(
  new THREE.Vector3(0, 0, 0),
  new THREE.Vector3(2.8, 0.8, -0.3),
  new THREE.Vector3(5.5, 0, 0)
);

const DAG_PATH = new THREE.QuadraticBezierCurve3(
  new THREE.Vector3(5.5, 1.8, 0),
  new THREE.Vector3(5.5, 0.6, 0.2),
  new THREE.Vector3(4.0, -0.6, 0.3)
);

export default function DataFlowDots() {
  const phase1Refs = useRef<(THREE.Mesh | null)[]>([]);
  const phase2Refs = useRef<(THREE.Mesh | null)[]>([]);
  const dagRefs = useRef<(THREE.Mesh | null)[]>([]);

  const phase1Progress = useRef<number[]>(
    Array.from({ length: PHASE1_COUNT }, (_, i) => i / PHASE1_COUNT)
  );
  const phase2Progress = useRef<number[]>(
    Array.from({ length: PHASE2_COUNT }, (_, i) => i / PHASE2_COUNT)
  );
  const dagProgress = useRef<number[]>(
    Array.from({ length: DAG_PARTICLE_COUNT }, (_, i) => i / DAG_PARTICLE_COUNT)
  );

  const tempVec = useMemo(() => new THREE.Vector3(), []);

  useFrame((_, delta) => {
    phase1Progress.current = phase1Progress.current.map((p, i) => {
      const next = (p + delta * 0.18) % 1;
      const mesh = phase1Refs.current[i];
      if (mesh) {
        INGESTION_TO_CORE.getPoint(next, tempVec);
        mesh.position.copy(tempVec);
        const fade = Math.sin(next * Math.PI);
        (mesh.material as THREE.MeshStandardMaterial).opacity = Math.max(0.1, fade * 0.9);
      }
      return next;
    });

    phase2Progress.current = phase2Progress.current.map((p, i) => {
      const next = (p + delta * 0.22) % 1;
      const mesh = phase2Refs.current[i];
      if (mesh) {
        CORE_TO_DAG.getPoint(next, tempVec);
        mesh.position.copy(tempVec);
        const fade = Math.sin(next * Math.PI);
        (mesh.material as THREE.MeshStandardMaterial).opacity = Math.max(0.08, fade * 0.85);
      }
      return next;
    });

    dagProgress.current = dagProgress.current.map((p, i) => {
      const next = (p + delta * 0.28) % 1;
      const mesh = dagRefs.current[i];
      if (mesh) {
        DAG_PATH.getPoint(next, tempVec);
        mesh.position.copy(tempVec);
        const fade = Math.sin(next * Math.PI);
        (mesh.material as THREE.MeshStandardMaterial).opacity = Math.max(0.08, fade * 0.7);
      }
      return next;
    });
  });

  return (
    <group>
      {/* Phase 1 — tangerine: ingestion → core */}
      {Array.from({ length: PHASE1_COUNT }).map((_, i) => (
        <mesh key={`p1-${i}`} ref={el => { phase1Refs.current[i] = el; }}>
          <sphereGeometry args={[0.065, 8, 8]} />
          <meshStandardMaterial
            color="#F97316"
            emissive="#EA580C"
            emissiveIntensity={0.4}
            transparent
            opacity={0.9}
          />
        </mesh>
      ))}

      {/* Phase 2 — cobalt: core → DAG */}
      {Array.from({ length: PHASE2_COUNT }).map((_, i) => (
        <mesh key={`p2-${i}`} ref={el => { phase2Refs.current[i] = el; }}>
          <sphereGeometry args={[0.055, 8, 8]} />
          <meshStandardMaterial
            color="#2563EB"
            emissive="#1D4ED8"
            emissiveIntensity={0.4}
            transparent
            opacity={0.85}
          />
        </mesh>
      ))}

      {/* DAG population */}
      {Array.from({ length: DAG_PARTICLE_COUNT }).map((_, i) => (
        <mesh key={`dag-${i}`} ref={el => { dagRefs.current[i] = el; }}>
          <sphereGeometry args={[0.045, 8, 8]} />
          <meshStandardMaterial color="#1E40AF" transparent opacity={0.7} />
        </mesh>
      ))}
    </group>
  );
}
