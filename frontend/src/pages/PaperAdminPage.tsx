import { Navigate } from "react-router-dom";

export function PaperAdminPage() {
  return <Navigate to="/admin?section=markets" replace />;
}
