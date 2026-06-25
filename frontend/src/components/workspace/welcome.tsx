"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useMemo } from "react";

import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

import { AuroraText } from "../ui/aurora-text";

let waved = false;

/** 取当前时段：早晨/下午/晚上。
 *  参数(输入): 无（读本地时间）。
 *  返回(输出): "morning" | "afternoon" | "evening"。 */
function currentPeriod(): "morning" | "afternoon" | "evening" {
  const h = new Date().getHours();
  if (h < 12) return "morning";
  if (h < 18) return "afternoon";
  return "evening";
}

/** 从邮箱推导一个展示名（@ 前缀，首字母大写）。
 *  参数(输入): email — 用户邮箱，可空。
 *  返回(输出): 展示名字符串，无邮箱时为空串。 */
function nameFromEmail(email: string | undefined): string {
  const local = email?.split("@")[0]?.trim();
  if (!local) return "";
  return local.charAt(0).toUpperCase() + local.slice(1);
}

export function Welcome({
  className,
  mode,
}: {
  className?: string;
  mode?: "ultra" | "pro" | "thinking" | "flash";
}) {
  const { t } = useI18n();
  const { user } = useAuth();
  const searchParams = useSearchParams();
  const isUltra = useMemo(() => mode === "ultra", [mode]);
  const greeting = useMemo(
    () => t.welcome.greeting(nameFromEmail(user?.email), currentPeriod()),
    [t, user?.email],
  );
  const colors = useMemo(() => {
    if (isUltra) {
      return ["#efefbb", "#e9c665", "#e3a812"];
    }
    return ["var(--color-foreground)"];
  }, [isUltra]);
  useEffect(() => {
    waved = true;
  }, []);
  return (
    <div
      className={cn(
        "mx-auto flex w-full flex-col items-center justify-center gap-2 px-8 py-4 text-center",
        className,
      )}
    >
      <div className="text-2xl font-bold">
        {searchParams.get("mode") === "skill" ? (
          `✨ ${t.welcome.createYourOwnSkill} ✨`
        ) : (
          <div className="flex items-center gap-2">
            {isUltra && (
              <div
                className={cn("inline-block", !waved ? "animate-wave" : "")}
              >
                🚀
              </div>
            )}
            <AuroraText colors={colors}>{greeting}</AuroraText>
          </div>
        )}
      </div>
      {searchParams.get("mode") === "skill" && (
        <div className="text-muted-foreground text-sm">
          {t.welcome.createYourOwnSkillDescription.includes("\n") ? (
            <pre className="font-sans whitespace-pre">
              {t.welcome.createYourOwnSkillDescription}
            </pre>
          ) : (
            <p>{t.welcome.createYourOwnSkillDescription}</p>
          )}
        </div>
      )}
    </div>
  );
}
