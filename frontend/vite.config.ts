import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// 开发模式（npm run dev）下，浏览器访问的是 Vite 开发服务器 http://localhost:5173。
// 前端代码里所有请求都写成相对路径（/api/...、/ws/...），由 Vite 的代理转发给后端，
// 这样开发环境和生产环境（Nginx 同源反代）的代码完全一致，也不需要处理 CORS。
//
// 默认转发到 docker compose 里的 Nginx（http://localhost:8080）。
// 想连别的后端（比如在本机直接用 uvicorn 启动、监听 8000 端口的 API）时，用环境变量覆盖：
//   DEV_PROXY_TARGET=http://localhost:8000 npm run dev
// 注意这个变量名故意不以 VITE_ 开头：VITE_ 开头的变量会被打包进前端代码，而它只给开发服务器用。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.DEV_PROXY_TARGET || 'http://localhost:8080';

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target, changeOrigin: true },
        // ws: true 让代理也转发 WebSocket 的 Upgrade 握手（对应生产环境 Nginx 的 /ws/ 配置）
        '/ws': { target, ws: true, changeOrigin: true },
      },
    },
    preview: {
      port: 4173,
      proxy: {
        '/api': { target, changeOrigin: true },
        '/ws': { target, ws: true, changeOrigin: true },
      },
    },
    build: {
      rollupOptions: {
        output: {
          // 把很少变化的第三方库拆成独立文件：业务代码改动后，
          // 浏览器仍能复用已缓存的 react / echarts 文件（文件名带内容哈希，见 nginx.conf 的长缓存规则）。
          manualChunks(id) {
            if (id.includes('node_modules/echarts') || id.includes('node_modules/zrender')) return 'echarts';
            if (id.includes('node_modules/react') || id.includes('node_modules/scheduler')) return 'react';
            return undefined;
          },
        },
      },
    },
  };
});
