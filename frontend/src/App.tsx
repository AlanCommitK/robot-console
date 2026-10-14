import { AuthProvider, useAuth } from './auth';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Dashboard } from './pages/Dashboard';
import { Login } from './pages/Login';

/** 只有两个“页面”，用登录状态切换即可，不需要引入路由库 */
function Root() {
  const { session } = useAuth();
  return session ? <Dashboard /> : <Login />;
}

export function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <Root />
      </AuthProvider>
    </ErrorBoundary>
  );
}
