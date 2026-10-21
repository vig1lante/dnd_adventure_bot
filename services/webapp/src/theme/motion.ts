import { useSyncExternalStore } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

// matchMedia нет в jsdom и в совсем старых WebView: тогда считаем, что движение разрешено.
function subscribe(onChange: () => void): () => void {
  const media = window.matchMedia?.(QUERY);
  media?.addEventListener("change", onChange);
  return () => media?.removeEventListener("change", onChange);
}

const getSnapshot = (): boolean => window.matchMedia?.(QUERY).matches ?? false;

/** Системная настройка «меньше движения»; обновляется, пока компонент на экране. */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

/** Класс анимации (`ui-anim-*`) или пустая строка, если движение нужно убрать. */
export function motionClass(name: string, reduced: boolean): string {
  return reduced ? "" : name;
}

/** Хук для компонентов: класс анимации, который сам исчезает при reduced motion. */
export function useMotionClass(name: string): string {
  return motionClass(name, useReducedMotion());
}
