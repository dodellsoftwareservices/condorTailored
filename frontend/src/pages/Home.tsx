import { Navigate, useLocation } from "react-router-dom";

import { Dashboard } from "@/pages/Dashboard";

/** Keep bookmarked agent conversations working after the dashboard becomes home. */
export function Home() {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  const chatLink = ["agent", "ask", "conversation", "panel", "desk"].some((key) => params.has(key))
    || ["chat", "fleet"].includes(params.get("view") || "");
  return chatLink ? <Navigate to={`/agents${search}`} replace /> : <Dashboard />;
}
