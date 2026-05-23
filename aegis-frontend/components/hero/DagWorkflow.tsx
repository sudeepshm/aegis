"use client";

import { useRef, useMemo } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";

interface DagNodeDef {
  id: number;
  pos: [number, number, number];
  parent: number | null;
  state: "compliant" | "high" | "critical";
  size: number;
}

const DAG_NODES: DagNodeDef[] = [
  { id: 0,  pos: [0,    1.8,  0],    parent: null, state: "compliant", size: 0.22 },
  { id: 1,  pos: [-1.5, 0.6,  0.3],  parent: 0,    state: "high",      size: 0.17 },
  { id: 2,  pos: [0,    0.6,  0],    parent: 0,    state: "compliant", size: 0.17 },
  { id: 3,  pos: [1.5,  0.6,  -0.3], parent: 0,    state: "compliant", size: 0.17 },
  { id: 4,  pos: [-2.2, -0.6, 0.5],  parent: 1,    state: "critical",  size: 0.14 },
  { id: 5,  pos: [-1.1, -0.6, 0.2],  parent: 1,    state: "high",      size: 0.13 },
  { id: 6,  pos: [-0.5, -0.6, 0],    parent: 2,    state: "compliant", size: 0.14 },
  { id: 7,  pos: [0.5,  -0.6, 0],    parent: 2,    state: "compliant", size: 0.13 },
  { id: 8,  pos: [1.1,  -0.6, -0.2], parent: 3,    state: "compliant", size: 0.14 },
  { id: 9,  pos: [2.2,  -0.6, -0.5], parent: 3,    state: "high",      size: 0.13 },
  { id: 10, pos: [-1.7, -1.8, 0.4],  parent: 4,    state: "critical",  size: 0.11 },
  { id: 11, pos: [0,    -1.8, 0],    parent: 6,    state: "compliant", size: 0.11 },
  { id: 12, pos: [1.7,  -1.8, -0.4], parent: 9,    state: "compliant", size: 0.11 },
];

const STATE_COLORS = {
  compliant: "#1E40AF",
  high:      "#F97316",
  critical:  "#EF4444",
};

function EdgeMesh({ start, end }: { start: [number,number,number]; end: [number,number,number] }) {
  const s = new THREE.Vector3(...start);
  const e = new THREE.Vector3(...end);
  const mid = s.clone().lerp(e, 0.5);
  const len = s.distanceTo(e);
  const quat = new THREE.Quaternion();
  quat.setFromUnitVectors(new THREE.Vector3(0, 1, 0), e.clone().sub(s).normalize());
  return (
    <mesh position={mid} quaternion={quat}>
      <cylinderGeometry args={[0.01, 0.01, len, 6]} />
      <meshStandardMaterial color="#94A3B8" transparent opacity={0.45} />
    </mesh>
  );
}

export default function DagWorkflow() {
  const groupRef = useRef<THREE.Group>(null);
  const nodeRefs = useRef<(THREE.Mesh | null)[]>([]);
  const cascadeTimer = useRef(0);
  const cascadeStage = useRef(-1);

  const edges = useMemo(() =>
    DAG_NODES.filter(n => n.parent !== null).map(n => ({
      from: n.parent!,
      to: n.id,
    })), []);

  useFrame((_, delta) => {
    cascadeTimer.current += delta;

    if (cascadeTimer.current > 6 && cascadeStage.current === -1) {
      cascadeTimer.current = 0;
      cascadeStage.current = 0;
    }
    if (cascadeStage.current === 0 && cascadeTimer.current > 1.2) cascadeStage.current = 1;
    if (cascadeStage.current === 1 && cascadeTimer.current > 2.4) cascadeStage.current = 2;
    if (cascadeStage.current === 2 && cascadeTimer.current > 4) { cascadeStage.current = -1; cascadeTimer.current = 0; }

    DAG_NODES.forEach((node, i) => {
      const mesh = nodeRefs.current[i];
      if (!mesh) return;
      const mat = mesh.material as THREE.MeshStandardMaterial;
      let targetHex = STATE_COLORS[node.state];

      if (cascadeStage.current === 0 && node.id === 4) targetHex = "#F97316";
      if (cascadeStage.current === 1 && [4, 10].includes(node.id)) targetHex = "#EF4444";
      if (cascadeStage.current === 2 && [1, 4, 5, 10].includes(node.id)) targetHex = "#EF4444";

      mat.color.lerp(new THREE.Color(targetHex), 0.08);
    });

    if (groupRef.current) groupRef.current.rotation.y += 0.003;
  });

  return (
    <group ref={groupRef} position={[5.5, 0, 0]}>
      {edges.map(e => (
        <EdgeMesh
          key={`e-${e.from}-${e.to}`}
          start={DAG_NODES[e.from].pos}
          end={DAG_NODES[e.to].pos}
        />
      ))}

      {DAG_NODES.map((node, i) => (
        <mesh key={node.id} ref={el => { nodeRefs.current[i] = el; }} position={node.pos} castShadow>
          <sphereGeometry args={[node.size, 16, 16]} />
          <meshStandardMaterial
            color={STATE_COLORS[node.state]}
            metalness={0.85}
            roughness={0.12}
            emissive={STATE_COLORS[node.state]}
            emissiveIntensity={0.06}
          />
        </mesh>
      ))}
    </group>
  );
}
