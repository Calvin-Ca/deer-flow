import { redirect } from "next/navigation";

/**
 * 站点根路由。
 *
 * 精简后前端只保留登录后的 harness（workspace），不再有营销落地页/博客/文档。
 * 根路径直接重定向到 /workspace；/workspace 自身的布局会按登录态再转向 /login 或 /setup。
 *
 * 参数：无
 * 返回：无（始终触发服务端重定向）
 */
export default function RootPage() {
  redirect("/workspace");
}
