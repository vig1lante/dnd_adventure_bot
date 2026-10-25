import {
  draftOf,
  encounterOf,
  exitOf,
  featureOf,
  itemOf,
  locationOf,
  npcOf,
  refsOf,
  relationOf,
  secretOf,
} from "./fixtures";

// Заглушки отвечают как сервер, только пока их формы совпадают с контрактом: поля сущностей — поля
// моделей движка (`dnd_engine.adventure.schema`), которые сервер отдаёт `model_dump(mode="json")`
// целиком, вместе с полями по умолчанию (дополнения dispatcher B4 §1 и B5 §1). Если поле потеряется
// в заглушке, тесты экранов перестанут ловить его потерю при сохранении.
const keys = (value: object) => Object.keys(value).sort();
const JOURNAL = ["public", "known_at_start"];

describe("заглушки совпадают с формами контракта", () => {
  test("черновик — поля дополнения B4 §1", () => {
    expect(keys(draftOf())).toEqual(
      [
        "id",
        "version",
        "title",
        "adventure_id",
        "journal_intro",
        "settings",
        "world",
        "deities",
        "factions",
        "plot",
        "locations",
        "npcs",
        "relations",
        "encounters",
        "monsters",
        "items",
        "revelations",
        "start_location",
        "finale_location",
        "locks",
        "wishes",
        "steps",
        "job",
      ].sort(),
    );
  });

  test("локация — Location движка с выходами, деталями, секретами, зонами и связями зон", () => {
    const location = locationOf();

    expect(keys(location)).toEqual(
      [
        ...JOURNAL,
        "id",
        "name",
        "region",
        "read_aloud",
        "dm_notes",
        "exits",
        "features",
        "secrets",
        "encounter_id",
        "zones",
        "links",
      ].sort(),
    );
    expect(location.zones).toEqual([{ id: "here", name: "здесь" }]);
    expect(location.links).toEqual([]);
    expect(location.encounter_id).toBeNull();
  });

  test("выход, деталь и секрет локации — Exit, Feature, LocationSecret", () => {
    expect(keys(exitOf())).toEqual(
      ["id", "to", "direction", "description", "locked_by", "hidden_dc"].sort(),
    );
    expect(keys(featureOf())).toEqual(
      [
        "id",
        "name",
        "description",
        "check",
        "reveals",
        "hidden",
        "locked_by",
      ].sort(),
    );
    expect(keys(secretOf())).toEqual(["id", "text", "check", "reveals"].sort());
  });

  test("NPC — Npc движка; связи формата v1 в конструкторе пусты", () => {
    const npc = npcOf();

    expect(keys(npc)).toEqual(
      [
        ...JOURNAL,
        "id",
        "name",
        "role",
        "appearance",
        "manner",
        "motive",
        "secret",
        "knows",
        "attitude",
        "relations",
        "location_id",
        "statblock",
      ].sort(),
    );
    expect(npc.relations).toEqual([]);
  });

  test("связь — Relation, предмет — Item с держателем", () => {
    expect(keys(relationOf())).toEqual(
      ["id", "a", "b", "kind", "note", "secret", "mutual"].sort(),
    );
    expect(keys(itemOf())).toEqual(
      [...JOURNAL, "id", "name", "description", "base_item", "holder"].sort(),
    );
    expect(itemOf().holder).toBe("location:crypt");
  });

  test("справочник ссылок — {ref, kind, name}: виды по порядку, имя или id (B5 §6)", () => {
    const draft = draftOf({
      locations: [locationOf({ id: "crypt", name: "" })],
      npcs: [npcOf()],
      items: [itemOf()],
      encounters: [encounterOf("crypt-ghouls")],
    });

    expect(refsOf(draft)).toEqual([
      { ref: "npc:marta", kind: "npc", name: "Марта" },
      { ref: "location:crypt", kind: "location", name: "crypt" },
      { ref: "item:crypt-key", kind: "item", name: "Ключ от склепа" },
      {
        ref: "encounter:crypt-ghouls",
        kind: "encounter",
        name: "crypt-ghouls",
      },
    ]);
  });
});
