import { useEffect, useState } from 'react';

/** 每隔 intervalMs 返回一次当前时间，用来驱动“3 秒前”这类相对时间和在线超时判断 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}
