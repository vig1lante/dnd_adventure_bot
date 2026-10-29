export interface SkeletonProps {
  /** Что грузится — для читалки («Загружаем игры…»); на экране вместо слов строки-заглушки. */
  label: string;
  rows?: number;
}

/**
 * Загрузка: строки той же формы, что у списка (название, подпись, кнопка), — экран не прыгает,
 * когда данные пришли. Скелетон стоит без движения: ему нечего гасить при «меньше движения».
 */
export function Skeleton({ label, rows = 3 }: SkeletonProps) {
  return (
    <div role="status" className="ui-skeleton">
      <span className="ui-visually-hidden">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="ui-skeleton__row" aria-hidden="true">
          <span className="ui-skeleton__line ui-skeleton__line--wide" />
          <span className="ui-skeleton__line" />
          <span className="ui-skeleton__button" />
        </div>
      ))}
    </div>
  );
}
