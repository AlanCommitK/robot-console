import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles.css';

const container = document.getElementById('root');
if (!container) throw new Error('#root element not found in index.html');

// StrictMode 只在开发模式生效：故意把组件挂载 → 卸载 → 再挂载一次，提前暴露“effect 没写清理函数”之类的问题
// （所以开发时会看到 WebSocket 连了又断、再连一次，这是正常的）
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
