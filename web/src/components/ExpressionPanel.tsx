import type { ExprSlot } from "../types";

interface Props {
  slots: ExprSlot[];
  state: Record<string, string>;
  onPick: (slot: string, option: string) => void;
}

/** 表情面板（预留接口：槽位 + 互斥选项，与 YSM 的 scale=0 显隐一一对应） */
export default function ExpressionPanel({ slots, state, onPick }: Props) {
  return (
    <div className="panel">
      <div className="panel-title">表情（预留接口）</div>
      {slots.map((s) => (
        <div key={s.slot} className="expr-row">
          <span className="expr-label">{s.label}</span>
          <div className="expr-opts">
            {s.options.map((o) => (
              <button
                key={o.name}
                className={"chip" + (state[s.slot] === o.name ? " on" : "")}
                onClick={() => onPick(s.slot, o.name)}
              >
                {o.name}
              </button>
            ))}
          </div>
        </div>
      ))}
      <div className="hint">表情只切换骨骼显隐，不影响关节角</div>
    </div>
  );
}
