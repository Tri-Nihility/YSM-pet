// 与 Python 后端共享的类型

export interface JointDef {
  name: string;
  label: string;
  bone: string;
  limits: [number, number];
  default: number;
}

export interface ConfigResp {
  jointOrder: string[];
  joints: JointDef[];
  standHeight: number;
  segments: string[];
  useNativeAnimation: boolean;
}

export interface ExprOption {
  name: string;
  bones: string[];
}

export interface ExprSlot {
  slot: string;
  label: string;
  default?: string;
  options: ExprOption[];
}

export interface Rig {
  model: string;
  jointOrder: string[];
  joints: JointDef[];
  segments: string[];
  expressions: { enabled: boolean; slots: ExprSlot[] };
  hiddenBones: string[];
}

export interface WsFrame {
  t: number;
  mode: "manual" | "train" | "policy";
  stage: string;
  angles: number[];
  basePos: number[];
  baseQuat: number[];
  episode: number;
  step: number;
  reward: number;
  epRet: number;
  contacts: number[];
  running: boolean;
  paused: boolean;
  iter: number;
  iters: number;
  expressions: Record<string, string>;
}

export interface HistPoint {
  iter: number;
  ret: number;
  len: number;
  fallen: number;
}
