import type { JointDef } from "../types";

interface Props {
  joints: JointDef[];
  angles: number[];
  disabled: boolean;
  onChange: (idx: number, value: number) => void;
  onReset: () => void;
}

/** 九个关节滑块（左肘/右肘/左膝/右膝/左肩/右肩/左大腿根/右大腿根/脖子） */
export default function JointPanel({ joints, angles, disabled, onChange, onReset }: Props) {
  return (
    <div className="panel">
      <div className="panel-title">
        关节摆 Pose
        <button className="mini" disabled={disabled} onClick={onReset}>复位</button>
      </div>
      {joints.map((j, i) => {
        const [lo, hi] = j.limits;
        return (
          <div key={j.name} className="joint-row">
            <span className="joint-label">{j.label}</span>
            <input
              type="range"
              min={lo}
              max={hi}
              step={0.5}
              value={angles[i] ?? j.default}
              disabled={disabled}
              onChange={(e) => onChange(i, Number(e.target.value))}
            />
            <span className="joint-val">{(angles[i] ?? j.default).toFixed(0)}°</span>
          </div>
        );
      })}
      <div className="hint">滑块范围即 URDF 限位；训练运行时锁定</div>
    </div>
  );
}
