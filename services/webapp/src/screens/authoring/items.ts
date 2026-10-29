import type { Draft, Feature, Item, Location } from "../../api/types";

/** Приставка держателя-локации: `location:<id>`. */
export const LOCATION = "location:";

/** Деталь-тайник, которая раскрывает предмет (`reveals: item`); нет — предмет не спрятан. */
export const hidingFeature = (
  item: Item,
  locations: readonly Location[],
): Feature | undefined =>
  locations
    .flatMap((location) => location.features)
    .find((feature) =>
      feature.reveals.some(
        (reveal) => reveal.kind === "item" && reveal.id === item.id,
      ),
    );

/**
 * Предметы, которые лежат открыто: держатель — локация, и ни одна деталь их не прячет (B6 §6).
 * `place` — название локации для людей.
 */
export function openItems(draft: Draft): { item: Item; place: string }[] {
  return draft.items.flatMap((item) => {
    if (
      !item.holder.startsWith(LOCATION) ||
      hidingFeature(item, draft.locations) !== undefined
    ) {
      return [];
    }
    const id = item.holder.slice(LOCATION.length);
    const location = draft.locations.find((entry) => entry.id === id);
    return [{ item, place: location?.name || id }];
  });
}
