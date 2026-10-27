// Русские названия вариантов по id из правил создания (creation-options).

type Named = { id: string; name: string };

/** Название по id; неизвестный id остаётся как есть: лучше показать id, чем потерять строку. */
export const nameIn = (items: readonly Named[], id: string): string =>
  items.find((item) => item.id === id)?.name ?? id;

/** Названия по списку id, в том же порядке. */
export const namesIn = (items: readonly Named[], ids: readonly string[]) =>
  ids.map((id) => nameIn(items, id));
