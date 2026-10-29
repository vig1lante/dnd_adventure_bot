import type { ReactNode } from "react";
import { classNames } from "./classNames";
import { Icon } from "./icons";
import type { IconName } from "./icon-names";
import { onTabKeyDown, tabIds } from "./tabs";

export interface TabBarItem<Id extends string = string> {
  id: Id;
  /** Имя вкладки — ровно слово («Бой»): на точные имена опираются сквозные тесты. */
  label: string;
  icon: IconName;
  /** Отметка нового — число или «!»; нет — нового нет. Текста событий здесь не бывает. */
  badge?: string;
}

export interface TabBarProps<Id extends string> {
  /** Имя набора вкладок для читалки («Разделы игры»). */
  label: string;
  /** Общая часть id вкладок и панелей (useId экрана); панели — `TabPanel` с тем же `idBase`. */
  idBase: string;
  items: readonly TabBarItem<Id>[];
  selected: Id;
  onSelect: (id: Id) => void;
  /** Спрятать панель: на телефоне открыта клавиатура, и панель только заслоняла бы поле. */
  hidden?: boolean;
}

/**
 * Нижняя панель вкладок у большого пальца: иконка и подпись, у выбранной — подсветка. Бейдж
 * нового стоит рядом с кнопкой, а не в ней: имя вкладки остаётся ровно словом, а читалка узнаёт
 * о новом из описания «есть новое». У открытой вкладки бейджа нет — её и так видно.
 */
export function TabBar<Id extends string>({
  label,
  idBase,
  items,
  selected,
  onSelect,
  hidden = false,
}: TabBarProps<Id>) {
  const pick = (index: number) => onSelect(items[index].id);
  const newsId = `${idBase}-news`;
  return (
    <div className="ui-tabbar" hidden={hidden}>
      <div role="tablist" aria-label={label} className="ui-tabbar__list">
        {items.map((item, index) => {
          const current = item.id === selected;
          const badge = current ? undefined : item.badge;
          return (
            <div key={item.id} role="none" className="ui-tabbar__cell">
              <button
                id={tabIds(idBase, item.id).tab}
                type="button"
                role="tab"
                aria-selected={current}
                aria-controls={tabIds(idBase, item.id).panel}
                aria-describedby={badge === undefined ? undefined : newsId}
                tabIndex={current ? 0 : -1}
                className="ui-tabbar__tab"
                onClick={() => onSelect(item.id)}
                onKeyDown={(event) =>
                  onTabKeyDown(event, index, items.length, pick)
                }
              >
                <Icon name={item.icon} size={22} />
                <span className="ui-tabbar__label">{item.label}</span>
              </button>
              {badge !== undefined && (
                <span className="ui-tabbar__badge" aria-hidden="true">
                  {badge}
                </span>
              )}
            </div>
          );
        })}
      </div>
      <span id={newsId} hidden>
        есть новое
      </span>
    </div>
  );
}

export interface TabPanelProps {
  idBase: string;
  id: string;
  selected: boolean;
  className?: string;
  children?: ReactNode;
}

/**
 * Панель вкладки: связана с ней по id, невыбранная скрыта (но остаётся смонтированной). Блоки
 * внутри идут колонкой с тем же шагом, что и блоки экрана.
 */
export function TabPanel({
  idBase,
  id,
  selected,
  className,
  children,
}: TabPanelProps) {
  const ids = tabIds(idBase, id);
  return (
    <div
      id={ids.panel}
      role="tabpanel"
      aria-labelledby={ids.tab}
      hidden={!selected}
      className={classNames("ui-tabpanel", className)}
    >
      {children}
    </div>
  );
}
