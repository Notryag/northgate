import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/console/",
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("recharts") || id.includes("d3-")) return "charts";
          if (id.includes("lucide-react")) return "icons";
          if (id.includes("react")) return "react";
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": process.env.NORTHGATE_API_TARGET ?? "http://127.0.0.1:8081",
    },
  },
});
