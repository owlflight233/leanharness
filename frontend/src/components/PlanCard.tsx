import { Check, Pencil, Play, RotateCcw, X } from "lucide-react";

import { Markdown } from "./Markdown";
import type { Plan } from "../api/plans";

export function PlanCard({
  plan,
  onConfirm,
  onResume,
  onReject,
  onEdit,
}: {
  plan: Plan;
  onConfirm: () => void;
  onResume: () => void;
  onReject: () => void;
  onEdit: (stepId: string, field: "title" | "instruction", value: string) => void;
}) {
  const editable = plan.state === "AWAITING_CONFIRMATION";
  return (
    <section className="plan-card" aria-label="执行计划">
      <div className="plan-card-heading">
        <div>
          <span className="plan-card-eyebrow">执行计划</span>
          <h3>{plan.title}</h3>
        </div>
        <span className={`plan-card-state state-${plan.state.toLowerCase()}`}>{plan.state}</span>
      </div>
      <div className="plan-card-markdown"><Markdown content={plan.source_markdown} /></div>
      <ol className="plan-card-steps">
        {plan.steps.map((step) => (
          <li key={step.id} className={step.state.toLowerCase()}>
            {editable ? (
              <input
                aria-label={`步骤 ${step.sequence} 标题`}
                value={step.title}
                onChange={(event) => onEdit(step.id, "title", event.target.value)}
              />
            ) : <strong>{step.title}</strong>}
            {editable ? (
              <textarea
                aria-label={`步骤 ${step.sequence} 说明`}
                value={step.instruction}
                onChange={(event) => onEdit(step.id, "instruction", event.target.value)}
              />
            ) : <p>{step.instruction}</p>}
            <small>{step.state}</small>
          </li>
        ))}
      </ol>
      <div className="plan-card-actions">
        {editable && <>
          <button type="button" className="quiet" onClick={onReject}><X size={14} />拒绝</button>
          <button type="button" className="primary" onClick={onConfirm}><Check size={14} />确认执行</button>
        </>}
        {plan.state === "PAUSED" && <button type="button" className="primary" onClick={onResume}><RotateCcw size={14} />恢复计划</button>}
        {plan.state === "RUNNING" && <span className="plan-card-running"><Play size={13} />正在执行</span>}
        {plan.state === "COMPLETED" && <span className="plan-card-done"><Check size={13} />已完成</span>}
        {editable && <span className="plan-card-hint"><Pencil size={12} />确认前可编辑</span>}
      </div>
    </section>
  );
}
