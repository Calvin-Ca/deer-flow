/**
 * B2-lite 组价收尾事件总线（COST_STEP_DISPLAY_PLAN §8 决策 2）。
 *
 * 组价卡片走到终态（done/blocked）时 emit **一次**；线程流 hook（`useThreadStream`）订阅后，
 * 自动向 agent 发一条「组价完成，请收尾」触发消息（`hide_from_ui`，用户不可见），拿到 agent 收尾总结。
 *
 * 设计取舍：卡片深埋在消息树里，拿不到线程的 `sendMessage`；用极轻量的模块级 pub/sub 解耦，
 * 避免把回调穿过整条渲染链（markdown-content → message-group → …）。**不动 backend 核心**，仅前端旁路。
 *
 * 红线：只在**本次交互真实完成**时 emit（在 resume 流的 done 分支），**不在重开会话/初次加载已完成会话时 emit**
 * ——重开对话不应重复触发 agent 收尾（收尾在原回合已发生）。
 */
export interface CostDonePayload {
  /** 组价会话 id。 */
  taskId: string;
  /** 总造价（done 且算出时为 number；blocked/未算出为 null）。 */
  total: number | null;
  /** 终态：`"done"` | `"blocked"`。 */
  status: string;
}

type Handler = (payload: CostDonePayload) => void;

const handlers = new Set<Handler>();

/** 卡片终态时调用：广播给所有订阅者（通常仅线程流 hook 一个）。 */
export function emitCostDone(payload: CostDonePayload): void {
  for (const handler of handlers) {
    handler(payload);
  }
}

/** 订阅组价终态事件；返回取消订阅函数（在 effect 清理里调用）。 */
export function onCostDone(handler: Handler): () => void {
  handlers.add(handler);
  return () => {
    handlers.delete(handler);
  };
}
