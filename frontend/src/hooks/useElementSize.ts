import { useEffect, useState, type RefObject } from 'react';

/** 监听元素尺寸（ResizeObserver），返回 { width, height }；元素还没挂载时为 0 */
export function useElementSize(ref: RefObject<HTMLElement>): { width: number; height: number } {
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const r = el.getBoundingClientRect();
      setSize((prev) =>
        Math.round(prev.width) === Math.round(r.width) && Math.round(prev.height) === Math.round(r.height)
          ? prev
          : { width: r.width, height: r.height },
      );
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}
