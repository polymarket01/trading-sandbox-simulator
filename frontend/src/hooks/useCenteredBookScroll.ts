import { useLayoutEffect, useRef, type KeyboardEvent, type PointerEvent } from 'react';

// Only deliberate input releases the midpoint anchor; browser scroll anchoring,
// row focus and DOM replacement must not be mistaken for user scrolling.
export function useCenteredBookScroll(bottom: boolean, resetKey: string) {
  const ref = useRef<HTMLDivElement>(null);
  const manual = useRef(false);
  const inputAt = useRef(0);
  const release = () => { manual.current = true; inputAt.current = performance.now(); };
  const center = () => {
    const node = ref.current;
    if (node && manual.current && performance.now()-inputAt.current > 300 && (bottom ? node.scrollHeight-node.clientHeight-node.scrollTop : node.scrollTop) <= 2) manual.current=false;
    if (node && !manual.current) node.scrollTop = bottom ? node.scrollHeight : 0;
  };
  useLayoutEffect(() => { manual.current = false; center(); }, [resetKey]);
  useLayoutEffect(center);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const wheel = (event: WheelEvent) => {
      if (node.scrollHeight > node.clientHeight+2 && ((event.deltaY < 0 && node.scrollTop > 0) || (event.deltaY > 0 && node.scrollTop < node.scrollHeight-node.clientHeight-1))) release();
    };
    // Non-passive capture runs before the browser's default scrolling action.
    node.addEventListener('wheel', wheel, { capture: true, passive: false });
    const observer = new ResizeObserver(center);
    observer.observe(node);
    if (node.firstElementChild) observer.observe(node.firstElementChild);
    return () => { observer.disconnect(); node.removeEventListener('wheel', wheel, true); };
  }, [resetKey]);
  const canScroll = () => !!ref.current && ref.current.scrollHeight > ref.current.clientHeight + 2;
  return {
    ref,
    style: { overflowAnchor: 'none' as const },
    tabIndex: 0,
    onTouchMove: () => { if (canScroll()) release(); },
    onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
      const node=event.currentTarget, rect=node.getBoundingClientRect();
      const scrollbarWidth = node.offsetWidth-node.clientWidth;
      const onScrollbar = (scrollbarWidth > 0 && event.clientX >= rect.right-scrollbarWidth) || (event.target === node && event.clientX >= rect.right-12);
      if (canScroll() && onScrollbar) release();
    },
    onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
      if (canScroll() && ['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key)) release();
    },
    onScroll: () => {
      const node=ref.current;
      if (!node) return;
      const distance=bottom ? node.scrollHeight-node.clientHeight-node.scrollTop : node.scrollTop;
      if (manual.current && distance <= 2 && performance.now()-inputAt.current > 300) manual.current=false;
      if (!manual.current && distance > 2) center();
    },
  };
}
