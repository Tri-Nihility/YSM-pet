import { useEffect, useRef } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { JointDef, WsFrame } from "../types";

/** 模型空间(Y-up,+X右,-Z前) -> pybullet 空间(Z-up,+X前) 的旋转，det=+1 */
const P = [
  [0, 0, -1],
  [-1, 0, 0],
  [0, 1, 0],
];
const Q_P = new THREE.Quaternion().setFromRotationMatrix(
  new THREE.Matrix4().set(
    P[0][0], P[0][1], P[0][2], 0,
    P[1][0], P[1][1], P[1][2], 0,
    P[2][0], P[2][1], P[2][2], 0,
    0, 0, 0, 1,
  ),
);
const P_INV = new THREE.Matrix3().set(
  P[0][0], P[1][0], P[2][0],
  P[0][1], P[1][1], P[2][1],
  P[0][2], P[1][2], P[2][2],
);

interface Props {
  joints: JointDef[];
  /** 最新一帧（由 App 的 WS 连接写入） */
  frameRef: React.MutableRefObject<WsFrame | null>;
  /** 需要隐藏的骨骼（表情） */
  exprHiddenRef: React.MutableRefObject<Set<string>>;
  /** 观察模式：train 跟随物理基座位姿，manual 固定原点 */
  followPhysicsRef: React.MutableRefObject<boolean>;
}

export default function Viewport(props: Props) {
  const mountRef = useRef<HTMLDivElement>(null);
  const modelRef = useRef<{
    scene: THREE.Group;
    byName: Map<string, THREE.Object3D>;
    meshes: { obj: THREE.Object3D; bone: string }[];
    motion: THREE.Group;
  } | null>(null);

  useEffect(() => {
    const mount = mountRef.current!;
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(mount.clientWidth, mount.clientHeight);
    renderer.shadowMap.enabled = false;
    mount.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1e24);
    const camera = new THREE.PerspectiveCamera(45, mount.clientWidth / mount.clientHeight, 0.05, 60);
    camera.position.set(-1.9, 1.5, -2.4);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(0, 1.05, 0);
    controls.update();

    scene.add(new THREE.AmbientLight(0xffffff, 1.1));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(-2, 4, -3);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x8899ff, 0.5);
    fill.position.set(2, 2, 3);
    scene.add(fill);

    const grid = new THREE.GridHelper(8, 40, 0x3a4150, 0x252b34);
    scene.add(grid);

    // conv（模型->pb 旋转） > motion（物理基座位姿） > YSM_Root
    const conv = new THREE.Group();
    conv.quaternion.copy(Q_P);
    const motion = new THREE.Group();
    conv.add(motion);
    scene.add(conv);

    new GLTFLoader().load("/model.glb", (gltf) => {
      const root = gltf.scene.getObjectByName("YSM_Root") ?? gltf.scene;
      motion.add(root);
      const byName = new Map<string, THREE.Object3D>();
      const meshes: { obj: THREE.Object3D; bone: string }[] = [];
      gltf.scene.traverse((o) => {
        byName.set(o.name, o);
        const bone = (o.userData as { bone?: string }).bone;
        if (bone) meshes.push({ obj: o, bone });
      });
      modelRef.current = { scene: gltf.scene, byName, meshes, motion };
    });

    let raf = 0;
    let lastVisible = -1;
    const animate = () => {
      raf = requestAnimationFrame(animate);
      const m = modelRef.current;
      const f = props.frameRef.current;
      if (m && f) {
        // 9 关节：bone.rotation.x = -q（上游 RotationValue 对 X 取负）
        const joints = props.joints;
        for (let i = 0; i < joints.length; i++) {
          const b = m.byName.get(joints[i].bone);
          if (!b) continue;
          b.rotation.order = "ZYX";
          b.rotation.set(-(f.angles[i] ?? 0) * (Math.PI / 180), 0, 0);
        }
        // 物理基座
        if (props.followPhysicsRef.current) {
          const q = new THREE.Quaternion(f.baseQuat[0], f.baseQuat[1], f.baseQuat[2], f.baseQuat[3]);
          m.motion.quaternion.copy(q);
          const p = new THREE.Vector3(f.basePos[0], f.basePos[1], f.basePos[2]).applyMatrix3(P_INV);
          m.motion.position.copy(p);
        } else {
          m.motion.quaternion.identity();
          m.motion.position.set(0, 0, 0);
        }
        // 表情显隐（骨骼级 primitive 开关）
        if (m.meshes.length !== lastVisible) {
          lastVisible = m.meshes.length;
        }
        for (const { obj, bone } of m.meshes) {
          obj.visible = !props.exprHiddenRef.current.has(bone);
        }
      }
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    const onResize = () => {
      camera.aspect = mount.clientWidth / mount.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(mount.clientWidth, mount.clientHeight);
    };
    window.addEventListener("resize", onResize);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      controls.dispose();
      renderer.dispose();
      mount.removeChild(renderer.domElement);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div ref={mountRef} style={{ position: "absolute", inset: 0 }} />;
}
