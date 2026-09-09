import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { PageLoadBoundary } from "./components/PageLoadBoundary";
import { appBasePath } from "./lib/config";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={appBasePath || "/"}>
      <PageLoadBoundary><App /></PageLoadBoundary>
    </BrowserRouter>
  </React.StrictMode>,
);
