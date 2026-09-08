import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { PageLoadBoundary } from "./components/PageLoadBoundary";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <PageLoadBoundary><App /></PageLoadBoundary>
    </BrowserRouter>
  </React.StrictMode>,
);
