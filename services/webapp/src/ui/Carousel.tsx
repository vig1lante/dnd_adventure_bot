import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent, MouseEvent, PointerEvent, ReactNode } from "react";
import { Button } from "./Button";

/** Сдвиг пальца или мыши (px), с которого жест — листание, а не нажатие. */
const SWIPE_MIN = 40;

type Arrow = "prev" | "next";

/** Начало жеста: какой указатель нажат и где. */
interface GestureStart {
  pointerId: number;
  x: number;
  y: number;
}

export interface CarouselItem {
  id: string;
  name: string;
}

export interface CarouselProps<T extends CarouselItem> {
  /** Имя карусели для чтения с экрана («Раса»). */
  label: string;
  items: readonly T[];
  /** С какой карточки начать (выбранный вариант); иначе с первой. */
  initialId?: string | null;
  /** Содержимое карточки показанного варианта. */
  children: (item: T) => ReactNode;
}

/**
 * Карусель: одна карточка на экране, листание стрелками, свайпом (основным указателем), точками
 * и клавишами ←/→ при фокусе внутри. Без зацикливания: на краях соответствующая стрелка
 * недоступна, и фокус с неё переходит на противоположную.
 */
export function Carousel<T extends CarouselItem>({
  label,
  items,
  initialId,
  children,
}: CarouselProps<T>) {
  const [position, setPosition] = useState(() =>
    Math.max(
      items.findIndex((item) => item.id === initialId),
      0,
    ),
  );
  const rootRef = useRef<HTMLDivElement>(null);
  const start = useRef<GestureStart | null>(null);
  // Снимает слушатели отпускания на window, поставленные при нажатии.
  const stopWatching = useRef<(() => void) | null>(null);
  // Свайп мышью заканчивается щелчком по тому, что под курсором: его гасим.
  const swiped = useRef(false);
  // Стрелка, которой отдать фокус после листания к краю (см. `go`).
  const refocus = useRef<Arrow | null>(null);

  const arrowButton = (arrow: Arrow) =>
    rootRef.current?.querySelector<HTMLButtonElement>(
      `[data-arrow="${arrow}"]`,
    ) ?? null;

  useLayoutEffect(() => {
    if (refocus.current === null) return;
    arrowButton(refocus.current)?.focus();
    refocus.current = null;
  });

  useEffect(() => {
    const watching = stopWatching;
    return () => watching.current?.();
  }, []);

  if (items.length === 0) return null;
  const last = items.length - 1;
  // Вариантов могло стать меньше: позиция не выходит за список.
  const index = Math.min(position, last);
  const item = items[index];

  const go = (next: number) => {
    const target = Math.min(Math.max(next, 0), last);
    // Стрелка у края станет недоступной, и браузер сбросил бы с неё фокус на body: клавиши
    // ←/→ перестали бы листать. Фокус переходит на противоположную стрелку (после отрисовки:
    // сейчас она может быть ещё недоступна).
    const focused = document.activeElement;
    if (target === last && focused === arrowButton("next")) {
      refocus.current = "prev";
    } else if (target === 0 && focused === arrowButton("prev")) {
      refocus.current = "next";
    }
    setPosition(target);
  };

  const onKeyDown = (event: KeyboardEvent) => {
    const step =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    go(index + step);
  };

  const onPointerDown = (event: PointerEvent) => {
    // Жест ведёт только основной указатель: первый палец или левая кнопка мыши. Второй палец,
    // правая и средняя кнопки точку начала не подменяют.
    if (!event.isPrimary) return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    stopWatching.current?.();
    const { pointerId } = event;
    start.current = { pointerId, x: event.clientX, y: event.clientY };
    // Отпускание где угодно, и за краем карусели, завершает жест: иначе точка начала устареет,
    // и следующее отпускание внутри карусели сочтётся свайпом. До window событие доходит
    // последним, после onPointerUp карусели.
    const finish = (end: globalThis.PointerEvent) => {
      if (end.pointerId !== pointerId) return;
      if (start.current?.pointerId === pointerId) start.current = null;
      stop();
    };
    const stop = () => {
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      stopWatching.current = null;
    };
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    stopWatching.current = stop;
  };

  const onPointerUp = (event: PointerEvent) => {
    const from = start.current;
    if (from === null || from.pointerId !== event.pointerId) return;
    start.current = null;
    const dx = event.clientX - from.x;
    const dy = event.clientY - from.y;
    if (Math.abs(dx) < SWIPE_MIN || Math.abs(dx) <= Math.abs(dy)) return;
    // Щелчок идёт сразу за отпусканием, в той же задаче; после неё нажатия снова доходят.
    swiped.current = true;
    setTimeout(() => {
      swiped.current = false;
    }, 0);
    go(index + (dx < 0 ? 1 : -1));
  };

  const onClickCapture = (event: MouseEvent) => {
    if (!swiped.current) return;
    swiped.current = false;
    event.preventDefault();
    event.stopPropagation();
  };

  return (
    <div
      role="group"
      aria-roledescription="карусель"
      aria-label={label}
      className="ui-carousel"
      ref={rootRef}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onClickCapture={onClickCapture}
    >
      <div className="ui-carousel__nav">
        <Button
          variant="secondary"
          className="ui-carousel__arrow"
          aria-label="Предыдущая"
          data-arrow="prev"
          disabled={index === 0}
          onClick={() => go(index - 1)}
        >
          ←
        </Button>
        <p className="ui-carousel__counter" aria-live="polite">
          {`${index + 1} из ${items.length}`}
        </p>
        <Button
          variant="secondary"
          className="ui-carousel__arrow"
          aria-label="Следующая"
          data-arrow="next"
          disabled={index === last}
          onClick={() => go(index + 1)}
        >
          →
        </Button>
      </div>
      <div
        role="group"
        aria-roledescription="карточка"
        aria-label={item.name}
        className="ui-carousel__slide"
      >
        {children(item)}
      </div>
      <div className="ui-carousel__dots">
        {items.map((dot, dotIndex) => (
          <button
            key={dot.id}
            type="button"
            className="ui-carousel__dot"
            aria-label={dot.name}
            aria-current={dotIndex === index || undefined}
            onClick={() => go(dotIndex)}
          />
        ))}
      </div>
    </div>
  );
}
