import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      // 浏览器只访问 Vite；/api 请求由 Vite 转发到 FastAPI，WebSocket 也一并代理。
      "/api": {
        target: "http://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
