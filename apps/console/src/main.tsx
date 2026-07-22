import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import { App } from "./App";
import { AuthProvider } from "./auth";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#167455",
          colorInfo: "#2f6fab",
          colorSuccess: "#23875d",
          colorWarning: "#ad741d",
          colorError: "#c63f3f",
          borderRadius: 6,
          fontSize: 13,
          colorBgLayout: "#f4f6f5",
          colorText: "#202724",
        },
        components: {
          Table: { cellPaddingBlockSM: 9, cellPaddingInlineSM: 12, headerBg: "#f1f3f2" },
          Menu: { darkItemBg: "#1d2522", darkSubMenuItemBg: "#18201d" },
        },
      }}
    >
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <BrowserRouter basename="/console">
            <App />
          </BrowserRouter>
        </AuthProvider>
      </QueryClientProvider>
    </ConfigProvider>
  </StrictMode>,
);
