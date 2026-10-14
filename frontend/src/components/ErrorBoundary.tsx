import { Component, type ErrorInfo, type ReactNode } from 'react';

interface State {
  error: Error | null;
}

/**
 * 错误边界：子组件渲染时抛出的异常会被这里接住，显示一个友好的错误页，而不是整页白屏。
 * React 目前只支持用 class 组件实现错误边界（getDerivedStateFromError / componentDidCatch 没有 Hook 版本）。
 * 注意它接不住事件回调和异步代码（fetch、setTimeout）里的异常——那些要在各自的 try/catch 里处理。
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('UI crashed:', error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="fatal">
          <h1>页面出错了</h1>
          <p className="mono">{this.state.error.message}</p>
          <button type="button" className="btn btn--primary" onClick={() => window.location.reload()}>
            刷新页面
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
